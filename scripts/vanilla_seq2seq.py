import datetime
import numpy as np
import tensorflow as tf
from models import VanillaSeq2SeqEncoder, scheduler_preprocess, scheduler_get_labels
from utils import load_embedding
from utils import Dataloader
from scripts import DefaultScript


class Script(DefaultScript):

    slug = 'seq2seq'

    def train(self):
        training_set = Dataloader(self.config)
        training_set.load_dataset('./data/train.bin')
        training_set.load_vocab('./data/default.voc', self.config.vocab_size)

        testing_set = Dataloader(self.config, testing_data=True)
        testing_set.load_dataset('data/test.bin')
        testing_set.load_vocab('./data/default.voc', self.config.vocab_size)

        main(self.config, training_set, testing_set)


def main(config, training_set, testing_set):
    training_set.set_preprocess_fn(scheduler_preprocess)
    training_set.set_special_tokens(['<pad>', '<unk>'])
    testing_set.set_preprocess_fn(scheduler_preprocess)
    testing_set.set_special_tokens(['<pad>', '<unk>'])

    scheduler_model = VanillaSeq2SeqEncoder(config.batch_size, config.vocab_size, config.embedding_size, config.hidden_size)
    _ = scheduler_model()
    scheduler_model.optimize(config.learning_rate)

    tf.summary.scalar("cost", scheduler_model.mse)

    nthreads_intra = config.nthreads // 2
    nthreads_inter = config.nthreads - config.nthreads // 2

    with tf.Session(config=tf.ConfigProto(inter_op_parallelism_threads=nthreads_inter,
                                          intra_op_parallelism_threads=nthreads_intra)) as sess:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        writer = tf.summary.FileWriter('./logs/' + timestamp + '/train/', sess.graph)
        test_writer = tf.summary.FileWriter('./logs/' + timestamp + '/test/', sess.graph)
        saver = tf.train.Saver()
        sess.run(tf.global_variables_initializer())

        # Load word2vec pretrained embeddings
        load_embedding(sess, training_set.word_to_index, scheduler_model.word_embeddings, config.embedding_path,
                       config.embedding_size, config.vocab_size)

        for epoch in range(config.n_epochs):
            if not epoch % config.test_every:
                # Testing phase
                success = 0
                total = 0
                for k in range(0, len(testing_set), config.batch_size):
                    if k + config.batch_size < len(testing_set):
                        batch_endings1, batch_endings2, correct_ending = testing_set.get(k, config.batch_size,
                                                                                         random=True)
                        total += config.batch_size
                        shuffled_batch1, labels1 = scheduler_get_labels(batch_endings1)
                        shuffled_batch2, labels2 = scheduler_get_labels(batch_endings2)
                        probabilities1 = sess.run(
                            'scheduler/order_probability:0',
                            {'scheduler/x:0': shuffled_batch1,
                             'scheduler/optimize/label:0': labels1})
                        probabilities2 = sess.run(
                            'scheduler/order_probability:0',
                            {'scheduler/x:0': shuffled_batch2,
                             'scheduler/optimize/label:0': labels2})
                        for b in range(config.batch_size):
                            if probabilities1[b][np.where(labels1[b] == 1)[0][0]] > probabilities2[b][np.where(labels2[b] == 1)[0][0]]:
                                if correct_ending[b] == 0:
                                    success += 1
                            else:
                                if correct_ending[b] == 1:
                                    success += 1
                accuracy = float(success) / float(total)
                accuracy_summary = tf.Summary()
                accuracy_summary.value.add(tag='accuracy', simple_value=accuracy)
                test_writer.add_summary(accuracy_summary, epoch)
            for k in range(0, len(training_set), config.batch_size):
                if k + config.batch_size < len(training_set):
                    summary_op = tf.summary.merge_all()

                    batch = training_set.get(k, config.batch_size, random=True)
                    shuffled_batch, labels = scheduler_get_labels(batch)
                    probabilities, _, computed_mse, summary = sess.run(
                        ['scheduler/order_probability:0', 'scheduler/optimize/optimizer',
                         'scheduler/optimize/mse:0', summary_op],
                        {'scheduler/x:0': shuffled_batch,
                         'scheduler/optimize/label:0': labels})
                    writer.add_summary(summary, epoch * len(training_set) + k)
                    if not epoch % config.save_model_every:
                        model_path = './builds/' + timestamp
                        saver.save(sess, model_path, global_step=epoch)
            training_set.shuffle_lines()
            if not epoch % config.save_model_every:
                model_path = './builds/' + timestamp + '/model'
                saver.save(sess, model_path, global_step=epoch)
