from transformers import AutoModelForSequenceClassification, Trainer, TrainingArguments, AutoTokenizer, TrainerCallback
from sklearn.metrics import accuracy_score, precision_recall_fscore_support
from datasets import load_from_disk
import random
import logging
import boto3
from sagemaker.session import Session
import sys
import argparse
import os
import torch
from smdebug import modes
import smdebug.pytorch as smd

class DebuggerCallback(TrainerCallback):
    def __init__(self, hook=None):
        self.hook = hook
        
    def on_init_end(self, args, state, control, **kwargs):
        if self.hook is None:
            boto_session = boto3.session.Session(region_name=os.environ["AWS_REGION"])
            sagemaker_session = Session(boto_session=boto_session)
            self.hook = smd.Hook.create_from_json_file()
            self.hook.register_hook(model)
            
    def on_train_begin(self, args, state, control, **kwargs):
        self.hook.set_mode(smd.modes.TRAIN)
    def on_prediction_step(self, args, state, control, **kwargs):
        self.hook.set_mode(smd.modes.EVAL)

if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    # Hyperparameters sent by the client are passed as command-line arguments to the script
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--train_batch_size", type=int, default=32)
    parser.add_argument("--eval_batch_size", type=int, default=64)
    parser.add_argument("--warmup_steps", type=int, default=500)
    parser.add_argument("--model_name", type=str)
    parser.add_argument("--learning_rate", type=str, default=5e-5)

    # Data, model, and output directories
    parser.add_argument("--output_data_dir", type=str, default=os.environ["SM_OUTPUT_DATA_DIR"])
    parser.add_argument("--model_dir", type=str, default=os.environ["SM_MODEL_DIR"])
    parser.add_argument("--n_gpus", type=str, default=os.environ["SM_NUM_GPUS"])
    parser.add_argument("--training_dir", type=str, default=os.environ["SM_CHANNEL_TRAIN"])
    parser.add_argument("--test_dir", type=str, default=os.environ["SM_CHANNEL_TEST"])

    args, _ = parser.parse_known_args()
    
    # Creating a SageMaker Debugger hook. Refer `API for Saving Tensors` page for more on this
    hook = smd.Hook(out_dir=args.output_data_dir)

    # Set up logging
    logger = logging.getLogger(__name__)

    logging.basicConfig(
        level=logging.getLevelName("INFO"),
        handlers=[logging.StreamHandler(sys.stdout)],
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Load datasets
    train_dataset = load_from_disk(args.training_dir)
    test_dataset = load_from_disk(args.test_dir)

    logger.info(f"Loaded train_dataset length is: {len(train_dataset)}")
    logger.info(f"Loaded test_dataset length is: {len(test_dataset)}")

    # Compute metrics function for binary classification
    def compute_metrics(pred):
        labels = pred.label_ids
        preds = pred.predictions.argmax(-1)
        precision, recall, f1, _ = precision_recall_fscore_support(labels, preds, average="binary")
        acc = accuracy_score(labels, preds)
        return {"accuracy": acc, "f1": f1, "precision": precision, "recall": recall}

    # Download model from model hub
    model = AutoModelForSequenceClassification.from_pretrained(args.model_name)
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)

    # Define training args
    training_args = TrainingArguments(
        output_dir=args.model_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.train_batch_size,
        per_device_eval_batch_size=args.eval_batch_size,
        warmup_steps=args.warmup_steps,
        evaluation_strategy="epoch",
        logging_steps = 20,
        save_steps= 20,
        logging_dir=f"{args.output_data_dir}/logs",
        learning_rate=float(args.learning_rate),
    )

    # Create Trainer instance
    trainer = Trainer(
        model=model,
        args=training_args,
        compute_metrics=compute_metrics,
        train_dataset=train_dataset,
        eval_dataset=test_dataset,
        tokenizer=tokenizer,
        callbacks=[
            DebuggerCallback
        ]
    )

    # Train model
    trainer.train()

    # Evaluate model
    eval_result = trainer.evaluate(eval_dataset=test_dataset)

    # Write eval result to file which can be accessed later in S3 ouput
    with open(os.path.join(args.output_data_dir, "eval_results.txt"), "w") as writer:
        print(f"***** Eval results *****")
        for key, value in sorted(eval_result.items()):
            writer.write(f"{key} = {value}\n")

    # Save the model to s3
    trainer.save_model(args.model_dir)
