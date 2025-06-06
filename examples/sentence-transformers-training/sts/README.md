# Semantic Textual Similarity

Semantic Textual Similarity (STS) assigns a score on the similarity of two texts. In this example, we use the [stsb](https://huggingface.co/datasets/sentence-transformers/stsb) dataset as training data to fine-tune our model. See the following example scripts how to tune SentenceTransformer on STS data:

- **[training_stsbenchmark.py](training_stsbenchmark.py)** - This example shows how to create a SentenceTransformer model from scratch by using a pre-trained transformer model (e.g. [`distilbert-base-uncased`](https://huggingface.co/distilbert/distilbert-base-uncased)) together with a pooling layer.
- **[training_stsbenchmark_continue_training.py](training_stsbenchmark_continue_training.py)** - This example shows how to continue training on STS data for a previously created & trained SentenceTransformer model (e.g. [`all-mpnet-base-v2`](https://huggingface.co/sentence-transformers/all-mpnet-base-v2)).

## Requirements

First, you should install the requirements:
```bash
pip install -r requirements.txt
```

# General Models

## Single-card Training

To fine tune on the STS task:

1. Choose a pre-trained model `<model_name>` (for example: [bert-base-uncased](https://huggingface.co/google-bert/bert-base-uncased)).

2. Load the training, validation, and test datasets. Here, we use the STS benchmark dataset.

```python
train_dataset = load_dataset("sentence-transformers/stsb", split="train")
eval_dataset = load_dataset("sentence-transformers/stsb", split="validation")
test_dataset = load_dataset("sentence-transformers/stsb", split="test")
```

3. Execute the script:

```bash
PT_HPU_LAZY_MODE=1 python training_stsbenchmark.py bert-base-uncased
```
If you want to save the checkpoints for training model you need using `--saving_model_checkpoints` in the command and same for all examples below.

## Multi-card Training

For multi-card training you can use the script of [gaudi_spawn.py](https://github.com/huggingface/optimum-habana/blob/main/examples/gaudi_spawn.py) to execute. There are two options to run the multi-card training by using '--use_deepspeed' or '--use_mpi'. We take the option of '--use_deepspeed' for our example of  multi-card training.

```bash
HABANA_VISIBLE_MODULES="2,3" PT_HPU_LAZY_MODE=1 python ../../gaudi_spawn.py --use_deepspeed --world_size 2 training_stsbenchmark.py bert-base-uncased
```


# Large Models (intfloat/e5-mistral-7b-instruct Model)

## Single-card Training with LoRA+gradient_checkpointing

Pretraining the `intfloat/e5-mistral-7b-instruct` model requires approximately 130GB of memory, which exceeds the capacity of a single HPU (Gaudi 2 with 98GB memory). To address this, we can utilize LoRA and gradient checkpointing techniques to reduce the memory requirements, making it feasible to train the model on a single HPU.

```bash
PT_HPU_LAZY_MODE=1 python training_stsbenchmark.py intfloat/e5-mistral-7b-instruct --peft --lora_target_modules "q_proj" "k_proj" "v_proj"
```

## Multi-card Training with Deepspeed Zero3

Pretraining the `intfloat/e5-mistral-7b-instruct` model requires approximately 130GB of memory, which exceeds the capacity of a single HPU (Gaudi 2 with 98GB memory). To address this, we will use the Zero3 stages of DeepSpeed (model parallelism) to reduce the memory requirements.

Our tests have shown that training this model requires at least four HPUs when using DeepSpeed Zero3.

```bash
PT_HPU_LAZY_MODE=1 python ../../gaudi_spawn.py --world_size 4 --use_deepspeed training_stsbenchmark.py intfloat/e5-mistral-7b-instruct --deepspeed ds_config.json --bf16 --no-use_hpu_graphs_for_training --learning_rate 1e-7
```

In the above command, we need to enable lazy mode with a learning rate of `1e-7` and configure DeepSpeed using the `ds_config.json` file. 

# Training data

Here is a simplified version of our training data:

```python
from datasets import Dataset

sentence1_list = ["My first sentence", "Another pair"]
sentence2_list = ["My second sentence", "Unrelated sentence"]
labels_list = [0.8, 0.3]
train_dataset = Dataset.from_dict({
    "sentence1": sentence1_list,
    "sentence2": sentence2_list,
    "label": labels_list,
})
# => Dataset({
#     features: ['sentence1', 'sentence2', 'label'],
#     num_rows: 2
# })
print(train_dataset[0])
# => {'sentence1': 'My first sentence', 'sentence2': 'My second sentence', 'label': 0.8}
print(train_dataset[1])
# => {'sentence1': 'Another pair', 'sentence2': 'Unrelated sentence', 'label': 0.3}
```

In the aforementioned scripts, we directly load the [stsb](https://huggingface.co/datasets/sentence-transformers/stsb) dataset:

```python
from datasets import load_dataset

train_dataset = load_dataset("sentence-transformers/stsb", split="train")
# => Dataset({
#     features: ['sentence1', 'sentence2', 'score'],
#     num_rows: 5749
# })
```

# Loss Function

<img src="https://raw.githubusercontent.com/UKPLab/sentence-transformers/master/docs/img/SBERT_Siamese_Network.png" alt="SBERT Siamese Network Architecture" width="250"/>

For each sentence pair, we pass sentence A and sentence B through the BERT-based model, which yields the embeddings _u_ und _v_. The similarity of these embeddings is computed using cosine similarity and the result is compared to the gold similarity score. Note that the two sentences are fed through the same model rather than two separate models. In particular, the cosine similarity for similar texts is maximized and the cosine similarity for dissimilar texts is minimized. This allows our model to be fine-tuned and to recognize the similarity of sentences.

For more details, see [Sentence-BERT: Sentence Embeddings using Siamese BERT-Networks](https://arxiv.org/abs/1908.10084).


