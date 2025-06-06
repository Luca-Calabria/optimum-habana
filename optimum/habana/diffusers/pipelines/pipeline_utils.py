# coding=utf-8
# Copyright 2022 The HuggingFace Inc. team.
# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import importlib
import inspect
import os
import sys
from typing import Optional, Union

import torch
from diffusers.pipelines import DiffusionPipeline
from diffusers.pipelines.pipeline_utils import _unwrap_model
from diffusers.utils.hub_utils import load_or_create_model_card, populate_model_card
from diffusers.utils.torch_utils import is_compiled_module
from huggingface_hub import create_repo

from optimum.utils import logging

from ...transformers.gaudi_configuration import GaudiConfig
from ...utils import to_device_dtype


logger = logging.get_logger(__name__)


GAUDI_LOADABLE_CLASSES = {
    "diffusers": {
        "ModelMixin": ["save_pretrained", "from_pretrained"],
        "SchedulerMixin": ["save_pretrained", "from_pretrained"],
        "DiffusionPipeline": ["save_pretrained", "from_pretrained"],
        "OnnxRuntimeModel": ["save_pretrained", "from_pretrained"],
    },
    "transformers": {
        "PreTrainedTokenizer": ["save_pretrained", "from_pretrained"],
        "PreTrainedTokenizerFast": ["save_pretrained", "from_pretrained"],
        "PreTrainedModel": ["save_pretrained", "from_pretrained"],
        "FeatureExtractionMixin": ["save_pretrained", "from_pretrained"],
        "ProcessorMixin": ["save_pretrained", "from_pretrained"],
        "ImageProcessingMixin": ["save_pretrained", "from_pretrained"],
    },
    "optimum.habana.diffusers.schedulers": {
        "GaudiDDIMScheduler": ["save_pretrained", "from_pretrained"],
        "GaudiEulerDiscreteScheduler": ["save_pretrained", "from_pretrained"],
        "GaudiFlowMatchEulerDiscreteScheduler": ["save_pretrained", "from_pretrained"],
        "GaudiEulerAncestralDiscreteScheduler": ["save_pretrained", "from_pretrained"],
    },
}

GAUDI_ALL_IMPORTABLE_CLASSES = {}
for library in GAUDI_LOADABLE_CLASSES:
    GAUDI_ALL_IMPORTABLE_CLASSES.update(GAUDI_LOADABLE_CLASSES[library])


def _fetch_class_library_tuple(module):
    # import it here to avoid circular import
    from diffusers import pipelines

    # register the config from the original module, not the dynamo compiled one
    not_compiled_module = _unwrap_model(module)
    library = not_compiled_module.__module__.split(".")[0]
    if library == "optimum":
        library = "optimum.habana.diffusers.schedulers"

    # check if the module is a pipeline module
    module_path_items = not_compiled_module.__module__.split(".")
    pipeline_dir = module_path_items[-2] if len(module_path_items) > 2 else None

    path = not_compiled_module.__module__.split(".")
    is_pipeline_module = pipeline_dir in path and hasattr(pipelines, pipeline_dir)

    # if library is not in GAUDI_LOADABLE_CLASSES, then it is a custom module.
    # Or if it's a pipeline module, then the module is inside the pipeline
    # folder so we set the library to module name.
    if is_pipeline_module:
        library = pipeline_dir
    elif library not in GAUDI_LOADABLE_CLASSES:
        library = not_compiled_module.__module__

    # retrieve class_name
    class_name = not_compiled_module.__class__.__name__

    return (library, class_name)


class GaudiDiffusionPipeline(DiffusionPipeline):
    """
    Extends the [`DiffusionPipeline`](https://huggingface.co/docs/diffusers/api/diffusion_pipeline) class:
    - The pipeline is initialized on Gaudi if `use_habana=True`.
    - The pipeline's Gaudi configuration is saved and pushed to the hub.

    Args:
        use_habana (bool, defaults to `False`):
            Whether to use Gaudi (`True`) or CPU (`False`).
        use_hpu_graphs (bool, defaults to `False`):
            Whether to use HPU graphs or not.
        gaudi_config (Union[str, [`GaudiConfig`]], defaults to `None`):
            Gaudi configuration to use. Can be a string to download it from the Hub.
            Or a previously initialized config can be passed.
        bf16_full_eval (bool, defaults to `False`):
            Whether to use full bfloat16 evaluation instead of 32-bit.
            This will be faster and save memory compared to fp32/mixed precision but can harm generated images.
        sdp_on_bf16 (bool, defaults to `False`):
            Whether to allow PyTorch to use reduced precision in the SDPA math backend.
    """

    def __init__(
        self,
        use_habana: bool = False,
        use_hpu_graphs: bool = False,
        gaudi_config: Union[str, GaudiConfig] = None,
        bf16_full_eval: bool = False,
        sdp_on_bf16: bool = False,
    ):
        DiffusionPipeline.__init__(self)

        if sdp_on_bf16:
            if hasattr(torch._C, "_set_math_sdp_allow_fp16_bf16_reduction"):
                torch._C._set_math_sdp_allow_fp16_bf16_reduction(True)

        self.use_habana = use_habana
        if self.use_habana:
            self.use_hpu_graphs = use_hpu_graphs
            if self.use_hpu_graphs:
                logger.info("Enabled HPU graphs.")
            else:
                logger.info("Enabled lazy mode because `use_hpu_graphs=False`.")

            self._device = torch.device("hpu")

            import diffusers

            # Patch for unconditional image generation
            from ..models import gaudi_unet_2d_model_forward

            diffusers.models.unets.unet_2d.UNet2DModel.forward = gaudi_unet_2d_model_forward

            if isinstance(gaudi_config, str):
                # Config from the Hub
                self.gaudi_config = GaudiConfig.from_pretrained(gaudi_config)
            elif isinstance(gaudi_config, GaudiConfig):
                # Config already initialized
                self.gaudi_config = copy.deepcopy(gaudi_config)
            else:
                raise ValueError(
                    f"`gaudi_config` must be a string or a GaudiConfig object but is {type(gaudi_config)}."
                )

            if self.gaudi_config.use_torch_autocast:
                if bf16_full_eval:
                    logger.warning(
                        "`use_torch_autocast` is True in the given Gaudi configuration but "
                        "`torch_dtype=torch.bfloat16` was given. Disabling mixed precision and continuing in bf16 only."
                    )
                    self.gaudi_config.use_torch_autocast = False

            # Workaround for Synapse 1.11 for full bf16 and Torch Autocast
            if bf16_full_eval or self.gaudi_config.use_torch_autocast:
                import diffusers

                from ..models import (
                    gaudi_unet_2d_condition_model_forward,
                )

                diffusers.models.unets.unet_2d_condition.UNet2DConditionModel.forward = (
                    gaudi_unet_2d_condition_model_forward
                )

            if self.use_hpu_graphs:
                try:
                    import habana_frameworks.torch as ht
                except ImportError as error:
                    error.msg = f"Could not import habana_frameworks.torch. {error.msg}."
                    raise error
                self.ht = ht
                self.hpu_stream = ht.hpu.Stream()
                self.cache = {}
            else:
                try:
                    import habana_frameworks.torch.core as htcore
                except ImportError as error:
                    error.msg = f"Could not import habana_frameworks.torch.core. {error.msg}."
                    raise error
                self.htcore = htcore
        else:
            if use_hpu_graphs:
                raise ValueError(
                    "`use_hpu_graphs` is True but `use_habana` is False, please set `use_habana=True` to use HPU"
                    " graphs."
                )
            if gaudi_config is not None:
                raise ValueError(
                    "Got a non-None `gaudi_config` but `use_habana` is False, please set `use_habana=True` to use this"
                    " Gaudi configuration."
                )
            logger.info("Running on CPU.")
            self._device = torch.device("cpu")

    def register_modules(self, **kwargs):
        for name, module in kwargs.items():
            # retrieve library
            if module is None or isinstance(module, (tuple, list)) and module[0] is None:
                register_dict = {name: (None, None)}
            else:
                library, class_name = _fetch_class_library_tuple(module)
                register_dict = {name: (library, class_name)}

            # save model index config
            self.register_to_config(**register_dict)

            # set models
            setattr(self, name, module)

    def save_pretrained(
        self,
        save_directory: Union[str, os.PathLike],
        safe_serialization: bool = True,
        variant: Optional[str] = None,
        push_to_hub: bool = False,
        **kwargs,
    ):
        """
        Save the pipeline and Gaudi configurations.
        More information [here](https://huggingface.co/docs/diffusers/api/diffusion_pipeline#diffusers.DiffusionPipeline.save_pretrained).

        Arguments:
            save_directory (`str` or `os.PathLike`):
                Directory to which to save. Will be created if it doesn't exist.
            safe_serialization (`bool`, *optional*, defaults to `True`):
                Whether to save the model using `safetensors` or the traditional PyTorch way (that uses `pickle`).
            variant (`str`, *optional*):
                If specified, weights are saved in the format pytorch_model.<variant>.bin.
            push_to_hub (`bool`, *optional*, defaults to `False`):
                Whether or not to push your model to the Hugging Face model hub after saving it. You can specify the
                repository you want to push to with `repo_id` (will default to the name of `save_directory` in your
                namespace).
            kwargs (`Dict[str, Any]`, *optional*):
                Additional keyword arguments passed along to the [`~utils.PushToHubMixin.push_to_hub`] method.
        """
        model_index_dict = dict(self.config)
        model_index_dict.pop("_class_name", None)
        model_index_dict.pop("_diffusers_version", None)
        model_index_dict.pop("_module", None)
        model_index_dict.pop("_name_or_path", None)

        if push_to_hub:
            commit_message = kwargs.pop("commit_message", None)
            private = kwargs.pop("private", False)
            create_pr = kwargs.pop("create_pr", False)
            token = kwargs.pop("token", None)
            repo_id = kwargs.pop("repo_id", save_directory.split(os.path.sep)[-1])
            repo_id = create_repo(repo_id, exist_ok=True, private=private, token=token).repo_id

        expected_modules, optional_kwargs = self._get_signature_keys(self)

        def is_saveable_module(name, value):
            if name not in expected_modules:
                return False
            if name in self._optional_components and value[0] is None:
                return False
            return True

        model_index_dict = {k: v for k, v in model_index_dict.items() if is_saveable_module(k, v)}

        for pipeline_component_name in model_index_dict.keys():
            sub_model = getattr(self, pipeline_component_name)
            model_cls = sub_model.__class__

            # Dynamo wraps the original model in a private class.
            # I didn't find a public API to get the original class.
            if is_compiled_module(sub_model):
                sub_model = _unwrap_model(sub_model)
                model_cls = sub_model.__class__

            save_method_name = None
            # search for the model's base class in GAUDI_LOADABLE_CLASSES
            for library_name, library_classes in GAUDI_LOADABLE_CLASSES.items():
                if library_name in sys.modules:
                    library = importlib.import_module(library_name)
                else:
                    logger.info(
                        f"{library_name} is not installed. Cannot save {pipeline_component_name} as {library_classes} from {library_name}"
                    )

                for base_class, save_load_methods in library_classes.items():
                    class_candidate = getattr(library, base_class, None)
                    if class_candidate is not None and issubclass(model_cls, class_candidate):
                        # if we found a suitable base class in GAUDI_LOADABLE_CLASSES then grab its save method
                        save_method_name = save_load_methods[0]
                        break
                if save_method_name is not None:
                    break

            if save_method_name is None:
                logger.warn(f"self.{pipeline_component_name}={sub_model} of type {type(sub_model)} cannot be saved.")
                # make sure that unsaveable components are not tried to be loaded afterward
                self.register_to_config(**{pipeline_component_name: (None, None)})
                continue

            save_method = getattr(sub_model, save_method_name)

            # Call the save method with the argument safe_serialization only if it's supported
            save_method_signature = inspect.signature(save_method)
            save_method_accept_safe = "safe_serialization" in save_method_signature.parameters
            save_method_accept_variant = "variant" in save_method_signature.parameters

            save_kwargs = {}
            if save_method_accept_safe:
                save_kwargs["safe_serialization"] = safe_serialization
            if save_method_accept_variant:
                save_kwargs["variant"] = variant

            save_method(os.path.join(save_directory, pipeline_component_name), **save_kwargs)

        # finally save the config
        self.save_config(save_directory)
        if hasattr(self, "gaudi_config"):
            self.gaudi_config.save_pretrained(save_directory)

        if push_to_hub:
            # Create a new empty model card and eventually tag it
            model_card = load_or_create_model_card(repo_id, token=token, is_pipeline=True)
            model_card = populate_model_card(model_card)
            model_card.save(os.path.join(save_directory, "README.md"))

            self._upload_folder(
                save_directory,
                repo_id,
                token=token,
                commit_message=commit_message,
                create_pr=create_pr,
            )

    def to(self, *args, **kwargs):
        """
        Intercept to() method and disable gpu-hpu migration before sending to diffusers
        """
        kwargs["hpu_migration"] = False
        return super().to(
            *args,
            **kwargs,
        )

    @classmethod
    def from_pretrained(cls, pretrained_model_name_or_path: Optional[Union[str, os.PathLike]], **kwargs):
        """
        More information [here](https://huggingface.co/docs/diffusers/api/diffusion_pipeline#diffusers.DiffusionPipeline.from_pretrained).
        """

        # Set the correct log level depending on the node
        # Already done in super().init() but we have to do it again
        # because we use optimum.utils.logging here and not
        # diffusers.utils.logging
        log_level = kwargs.pop("log_level", logging.INFO)
        logging.set_verbosity(log_level)
        logging.enable_default_handler()
        logging.enable_explicit_format()

        # Import diffusers.pipelines.pipeline_utils to override the values of LOADABLE_CLASSES and ALL_IMPORTABLE_CLASSES
        import diffusers.pipelines.pipeline_utils

        diffusers.pipelines.pipeline_utils.LOADABLE_CLASSES = GAUDI_LOADABLE_CLASSES
        diffusers.pipelines.pipeline_utils.ALL_IMPORTABLE_CLASSES = GAUDI_ALL_IMPORTABLE_CLASSES

        # Define a new kwarg here to know in the __init__ whether to use full bf16 precision or not
        bf16_full_eval = kwargs.get("torch_dtype", None) == torch.bfloat16
        kwargs["bf16_full_eval"] = bf16_full_eval

        # Need to load custom ops lists before instantiating htcore
        if kwargs.get("gaudi_config", None) is not None:
            if isinstance(kwargs["gaudi_config"], str):
                gaudi_config = GaudiConfig.from_pretrained(kwargs["gaudi_config"])
            else:
                gaudi_config = kwargs["gaudi_config"]
            gaudi_config.declare_autocast_bf16_fp32_ops()
            kwargs["gaudi_config"] = gaudi_config

        # Import htcore here to support model quantization
        import habana_frameworks.torch.core as htcore  # noqa: F401

        # Normally we just need to return super().from_pretrained.  However this is a
        # workaround for Transformers 4.49.0 issue (sub_model torch_dtype option ignored).
        # Note this issue is already fixed in 4.50.0dev working branch..
        model = super().from_pretrained(
            pretrained_model_name_or_path,
            **kwargs,
        )
        if bf16_full_eval:
            # Get the component names
            component_names = [name for name in model.__dict__ if not name.startswith("_")]
            # Iterate through the component names and fix dtype
            for name in component_names:
                component = getattr(model, name, None)
                if component is not None and hasattr(component, "dtype"):
                    component.to(torch.bfloat16)

        return model

    @classmethod
    def save_lora_weights(
        cls,
        save_directory: Union[str, os.PathLike],
        **kwargs,
    ):
        # Move all lora layers state dicts from HPU to CPU before saving
        for key in list(kwargs.keys()):
            if key.endswith("_lora_layers") and kwargs[key] is not None:
                kwargs[key] = to_device_dtype(kwargs[key], target_device=torch.device("cpu"))

        # Call diffusers' base class handler
        return super().save_lora_weights(
            save_directory,
            **kwargs,
        )

    """
    def save_lora_weights(
        cls,
        save_directory: Union[str, os.PathLike],
        unet_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        text_encoder_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        text_encoder_2_lora_layers: Dict[str, Union[torch.nn.Module, torch.Tensor]] = None,
        is_main_process: bool = True,
        weight_name: str = None,
        save_function: Callable = None,
        safe_serialization: bool = True,
    ):
        # Move the state dict from HPU to CPU before saving
        if unet_lora_layers:
            unet_lora_layers = to_device_dtype(unet_lora_layers, target_device=torch.device("cpu"))
        if text_encoder_lora_layers:
            text_encoder_lora_layers = to_device_dtype(text_encoder_lora_layers, target_device=torch.device("cpu"))
        if text_encoder_2_lora_layers:
            text_encoder_2_lora_layers = to_device_dtype(text_encoder_2_lora_layers, target_device=torch.device("cpu"))

        # text_encoder_2_lora_layers is only supported by some diffuser pipelines
        signature = inspect.signature(super().save_lora_weights)
        if "text_encoder_2_lora_layers" in signature.parameters:
            return super().save_lora_weights(
                save_directory,
                unet_lora_layers,
                text_encoder_lora_layers,
                text_encoder_2_lora_layers,
                is_main_process,
                weight_name,
                save_function,
                safe_serialization,
            )
        else:
            return super().save_lora_weights(
                save_directory,
                unet_lora_layers,
                text_encoder_lora_layers,
                is_main_process,
                weight_name,
                save_function,
                safe_serialization,
            )
    """
