#!/usr/bin/env python3
# Usage:
#  ./train --dataset <file|directory|glob>
# or
#  import train
#  train.train(dataset = <file|directory|glob>)

import argparse
import json
import os
import sys
import numpy as np
import tensorflow as tf
import time
import tqdm
from tensorflow.core.protobuf import rewriter_config_pb2

# Local imports
sys.path.append('src')
import model, sample, encoder
from load_dataset import load_dataset, Sampler
from accumulate import AccumulatingOptimizer
import memory_saving_gradients

CHECKPOINT_DIR = 'checkpoint'
SAMPLE_DIR = 'samples'


parser = argparse.ArgumentParser(
    description='Fine-tune GPT-2 on your custom dataset.',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter)

parser.add_argument('--dataset', metavar='PATH', type=str, required=True, help='Input file, directory, or glob pattern (utf-8 text, or preencoded .npz files).')
parser.add_argument('--model_name', metavar='MODEL', type=str, default='117M', help='Pretrained model name')
parser.add_argument('--combine', metavar='CHARS', type=int, default=50000, help='Concatenate input files with <|endoftext|> separator into chunks of this minimum size')

parser.add_argument('--batch_size', metavar='SIZE', type=int, default=1, help='Batch size')
parser.add_argument('--learning_rate', metavar='LR', type=float, default=0.00002, help='Learning rate for Adam')
parser.add_argument('--accumulate_gradients', metavar='N', type=int, default=1, help='Accumulate gradients across N minibatches.')
parser.add_argument('--memory_saving_gradients', default=False, action='store_true', help='Use gradient checkpointing to reduce vram usage.')
parser.add_argument('--only_train_transformer_layers', default=False, action='store_true', help='Restrict training to the transformer blocks.')
parser.add_argument('--optimizer', type=str, default='adam', help='Optimizer. <adam|sgd>.')
parser.add_argument('--noise', type=float, default=0.0, help='Add noise to input training data to regularize against typos.')

parser.add_argument('--top_k', type=int, default=40, help='K for top-k sampling.')
parser.add_argument('--top_p', type=float, default=0.0, help='P for top-p sampling. Overrides top_k if set > 0.')

parser.add_argument('--restore_from', type=str, default='latest', help='Either "latest", "fresh", or a path to a checkpoint file')
parser.add_argument('--run_name', type=str, default='run1', help='Run id. Name of subdirectory in checkpoint/ and samples/')
parser.add_argument('--sample_every', metavar='N', type=int, default=100, help='Generate samples every N steps')
parser.add_argument('--sample_length', metavar='TOKENS', type=int, default=1023, help='Sample this many tokens')
parser.add_argument('--sample_num', metavar='N', type=int, default=1, help='Generate this many samples')
parser.add_argument('--save_every', metavar='N', type=int, default=1000, help='Write a checkpoint every N steps')

parser.add_argument('--val_dataset', metavar='PATH', type=str, default=None, help='Dataset for validation loss, defaults to --dataset.')
parser.add_argument('--val_batch_size', metavar='SIZE', type=int, default=2, help='Batch size for validation.')
parser.add_argument('--val_batch_count', metavar='N', type=int, default=40, help='Number of batches for validation.')
parser.add_argument('--val_every', metavar='STEPS', type=int, default=0, help='Calculate validation loss every STEPS steps.')


def maketree(path):
    try:
        os.makedirs(path)
    except:
        pass


def randomize(context, hparams, p):
    if p > 0:
        mask = tf.random.uniform(shape=tf.shape(context)) < p
        noise = tf.random.uniform(shape=tf.shape(context), minval=0, maxval=hparams.n_vocab, dtype=tf.int32)
        return tf.where(mask, noise, context)
    else:
        return context

# Fine-tune GPT-2 on custom dataset
# dataset: string               | Input file, directory, or glob pattern (utf-8 text, or preencoded .npz files)
# model_name: string            | Pretrained model name
# combine: number               | Concatenate input files with <|endoftext|> separator into chunks of this minimum size
# batch_size: number            | Batch size
# learning_rate: number         | Learning rate for Adam
# accumulate_gradients: number  | Accumulate gradients across N minibatches
# memory_saving_gradients: bool | Use gradient checkpointing to reduce vram usage
# optimizer: string             | Optimizer. <adam|sgd>
# noise: number                 | Add noise to input training data to regularize against typos
# top_k: number                 | K for top-k sampling
# top_p: number                 | P for top-p sampling. Overrides top_k if set > 0
# restore_from: string          | Either "latest", "fresh", or a path to a checkpoint file
# run_name: string              | Run id. Name of subdirectory in checkpoint/ and samples/
# sample_every: number          | Generate samples every N steps
# sample_length: number         | Sample this many tokens
# sample_num: number            | Generate this many samples
# save_every: number            | Write a checkpoint every N steps
# val_dataset: string           | Dataset for validation loss, defaults to --dataset
# val_batch_size: number        | Batch size for validation
# val_batch_count: number       | Number of batches for validation
# val_every: number             | Calculate validation loss every STEPS steps
def train(dataset
         ,model_name = "117M"
         ,combine = 50000
         ,batch_size = 1
         ,learning_rate = 0.00002
         ,accumulate_gradients = 1
         ,memory_saving_gradients = False
         ,only_train_transformer_layers = False
         ,optimizer = "adam"
         ,noise = 0.0
         ,top_k = 40
         ,top_p = 0.0
         ,restore_from = "latest"
         ,run_name = "run1"
         ,sample_every = 100
         ,sample_length = 1023
         ,sample_num = 1
         ,save_every = 1000
         ,val_dataset = ""
         ,val_batch_size = 2
         ,val_batch_count = 40
         ,val_every = 0):
    enc = encoder.get_encoder(model_name)
    hparams = model.default_hparams()
    with open(os.path.join('models', model_name, 'hparams.json')) as f:
        hparams.override_from_dict(json.load(f))

    if sample_length > hparams.n_ctx:
        raise ValueError(
            "Can't get samples longer than window size: %s" % hparams.n_ctx)

    if model_name == '345M':
        memory_saving_gradients = True
        if optimizer == 'adam':
            only_train_transformer_layers = True

    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    config.graph_options.rewrite_options.layout_optimizer = rewriter_config_pb2.RewriterConfig.OFF
    with tf.Session(config=config) as sess:
        context = tf.placeholder(tf.int32, [batch_size, None])
        context_in = randomize(context, hparams, noise)
        output = model.model(hparams=hparams, X=context_in)
        loss = tf.reduce_mean(
            tf.nn.sparse_softmax_cross_entropy_with_logits(
                labels=context[:, 1:], logits=output['logits'][:, :-1]))

        if val_every > 0:
            val_context = tf.placeholder(tf.int32, [val_batch_size, None])
            val_output = model.model(hparams=hparams, X=val_context)
            val_loss = tf.reduce_mean(
                tf.nn.sparse_softmax_cross_entropy_with_logits(
                    labels=val_context[:, 1:], logits=val_output['logits'][:, :-1]))
            val_loss_summary = tf.summary.scalar('val_loss', val_loss)


        tf_sample = sample.sample_sequence(
            hparams=hparams,
            length=sample_length,
            context=context,
            batch_size=batch_size,
            temperature=1.0,
            top_k=top_k,
            top_p=top_p)

        all_vars = [v for v in tf.trainable_variables() if 'model' in v.name]
        train_vars = [v for v in all_vars if '/h' in v.name] if only_train_transformer_layers else all_vars

        if optimizer == 'adam':
            opt = tf.train.AdamOptimizer(learning_rate=learning_rate)
        elif optimizer == 'sgd':
            opt = tf.train.GradientDescentOptimizer(learning_rate=learning_rate)
        else:
            exit('Bad optimizer:', optimizer)

        if accumulate_gradients > 1:
            if memory_saving_gradients:
                exit("Memory saving gradients are not implemented for gradient accumulation yet.")
            opt = AccumulatingOptimizer(
                opt=opt,
                var_list=train_vars)
            opt_reset = opt.reset()
            opt_compute = opt.compute_gradients(loss)
            opt_apply = opt.apply_gradients()
            summary_loss = tf.summary.scalar('loss', opt_apply)
        else:
            if memory_saving_gradients:
                opt_grads = memory_saving_gradients.gradients(loss, train_vars)
            else:
                opt_grads = tf.gradients(loss, train_vars)
            opt_grads = list(zip(opt_grads, train_vars))
            opt_apply = opt.apply_gradients(opt_grads)
            summary_loss = tf.summary.scalar('loss', loss)

        summary_lr = tf.summary.scalar('learning_rate', learning_rate)
        summaries = tf.summary.merge([summary_lr, summary_loss])

        summary_log = tf.summary.FileWriter(
            os.path.join(CHECKPOINT_DIR, run_name))

        saver = tf.train.Saver(
            var_list=all_vars,
            max_to_keep=5,
            keep_checkpoint_every_n_hours=2)
        sess.run(tf.global_variables_initializer())

        if restore_from == 'latest':
            ckpt = tf.train.latest_checkpoint(
                os.path.join(CHECKPOINT_DIR, run_name))
            if ckpt is None:
                # Get fresh GPT weights if new run.
                ckpt = tf.train.latest_checkpoint(
                    os.path.join('models', model_name))
        elif restore_from == 'fresh':
            ckpt = tf.train.latest_checkpoint(
                os.path.join('models', model_name))
        else:
            ckpt = tf.train.latest_checkpoint(restore_from)
        print('Loading checkpoint', ckpt)
        saver.restore(sess, ckpt)

        print('Loading dataset...')
        chunks = load_dataset(enc, dataset, combine)
        data_sampler = Sampler(chunks)
        if val_every > 0:
            val_chunks = load_dataset(enc, val_dataset, combine) if val_dataset else chunks
        print('dataset has', data_sampler.total_size, 'tokens')
        print('Training...')

        if val_every > 0:
            # Sample from validation set once with fixed seed to make
            # it deterministic during training as well as across runs.
            val_data_sampler = Sampler(val_chunks, seed=1)
            val_batches = [[val_data_sampler.sample(1024) for _ in range(val_batch_size)]
                           for _ in range(val_batch_count)]

        counter = 1
        counter_path = os.path.join(CHECKPOINT_DIR, run_name, 'counter')
        if os.path.exists(counter_path):
            # Load the step number if we're resuming a run
            # Add 1 so we don't immediately try to save again
            with open(counter_path, 'r') as fp:
                counter = int(fp.read()) + 1

        def save():
            maketree(os.path.join(CHECKPOINT_DIR, run_name))
            print(
                'Saving',
                os.path.join(CHECKPOINT_DIR, run_name,
                             'model-{}').format(counter))
            saver.save(
                sess,
                os.path.join(CHECKPOINT_DIR, run_name, 'model'),
                global_step=counter)
            with open(counter_path, 'w') as fp:
                fp.write(str(counter) + '\n')

        def generate_samples():
            print('Generating samples...')
            context_tokens = data_sampler.sample(1)
            all_text = []
            index = 0
            while index < sample_num:
                out = sess.run(
                    tf_sample,
                    feed_dict={context: batch_size * [context_tokens]})
                for i in range(min(sample_num - index, batch_size)):
                    text = enc.decode(out[i])
                    text = '======== SAMPLE {} ========\n{}\n'.format(
                        index + 1, text)
                    all_text.append(text)
                    index += 1
            print(text)
            maketree(os.path.join(SAMPLE_DIR, run_name))
            with open(
                    os.path.join(SAMPLE_DIR, run_name,
                                 'samples-{}').format(counter), 'w') as fp:
                fp.write('\n'.join(all_text))

        def validation():
            print('Calculating validation loss...')
            losses = []
            for batch in tqdm.tqdm(val_batches):
                losses.append(sess.run(val_loss, feed_dict={val_context: batch}))
            v_val_loss = np.mean(losses)
            v_summary = sess.run(val_loss_summary, feed_dict={val_loss: v_val_loss})
            summary_log.add_summary(v_summary, counter)
            summary_log.flush()
            print(
                '[{counter} | {time:2.2f}] validation loss = {loss:2.2f}'
                .format(
                    counter=counter,
                    time=time.time() - start_time,
                    loss=v_val_loss))

        def sample_batch():
            return [data_sampler.sample(1024) for _ in range(batch_size)]


        avg_loss = (0.0, 0.0)
        start_time = time.time()

        try:
            while True:
                if counter % save_every == 0:
                    save()
                if counter % sample_every == 0:
                    generate_samples()
                if val_every > 0 and (counter % val_every == 0 or counter == 1):
                    validation()

                if accumulate_gradients > 1:
                    sess.run(opt_reset)
                    for _ in range(accumulate_gradients):
                        sess.run(
                            opt_compute, feed_dict={context: sample_batch()})
                    (v_loss, v_summary) = sess.run((opt_apply, summaries))
                else:
                    (_, v_loss, v_summary) = sess.run(
                        (opt_apply, loss, summaries),
                        feed_dict={context: sample_batch()})

                summary_log.add_summary(v_summary, counter)

                avg_loss = (avg_loss[0] * 0.99 + v_loss,
                            avg_loss[1] * 0.99 + 1.0)

                print(
                    '[{counter} | {time:2.2f}] loss={loss:2.2f} avg={avg:2.2f}'
                    .format(
                        counter=counter,
                        time=time.time() - start_time,
                        loss=v_loss,
                        avg=avg_loss[0] / avg_loss[1]))

                counter += 1
        except KeyboardInterrupt:
            print('interrupted')
            save()


if __name__ == '__main__':
    args = parser.parse_args()
    train(**args)