"""
Credits to
- A large annotated corpus for learning natural language inference,
    _Samuel R. Bowman, Gabor Angeli, Christopher Potts, and Christopher D. Manning_,
    https://nlp.stanford.edu/pubs/snli_paper.pdf.
- AllenNLP for Elmo Embeddings: Deep contextualized word representations
    _Matthew E. Peters, Mark Neumann, Mohit Iyyer, Matt Gardner, Christopher Clark, Kenton Lee, Luke Zettlemoyer_,
    https://arxiv.org/abs/1802.05365.
- Jacob Zweig for Elmo embedding import code from
https://towardsdatascience.com/elmo-embeddings-in-keras-with-tensorflow-hub-7eb6f0145440.
"""
import datetime
import os
import random

import keras
from keras import backend as K
import tensorflow as tf
import tensorflow_hub as hub
import numpy as np
from utils import SNLIDataloader, Dataloader
from nltk import word_tokenize
from scripts import DefaultScript


class Script(DefaultScript):
    slug = 'entailment_v3'

    def train(self):
        main(self.config)


def preprocess_fn(line):
    # label = [entailment, neutral, contradiction]
    label = 1
    if line['gold_label'] == 'contradiction':
        label = 0
    sentence1 = ' '.join(word_tokenize(line['sentence1']))
    sentence2 = ' '.join(word_tokenize(line['sentence2']))
    output = [label, sentence1, sentence2]
    return output


def output_fn(_, batch):
    batch = np.array(batch, dtype=object)
    return [batch[:, 1], batch[:, 2]], np.array(list(batch[:, 0]))


def output_fn_test(data):
    batch = np.array(data.batch)
    sentences1 = []
    sentences2 = []
    label = []
    for b in batch:
        sentence = " ".join(b[0]) + " "
        sentence += " ".join(b[1]) + " "
        sentence += " ".join(b[2]) + " "
        sentence += " ".join(b[3]) + " "
        if random.random() > 0.5:
            sentences2.append(" ".join(b[4]))
            label.append(2 - int(b[6][0]))
        else:
            sentences2.append(" ".join(b[5]))
            label.append(int(b[6][0]) - 1)
        sentences1.append(sentence)
    return [np.array(sentences1), np.array(sentences2)], np.array(label)


class ElmoEmbedding:
    def __init__(self, elmo_model, embedding_type):
        self.elmo_model = elmo_model
        self.embedding_type = embedding_type
        self.__name__ = "elmo_embeddings"

    def __call__(self, x):
        if self.embedding_type['name'] == "elmo":
            return self.elmo_model(tf.squeeze(tf.cast(x, tf.string)), signature="default", as_dict=True)[
                "default"]
        if self.embedding_type['name'] == "use":
            return self.elmo_model(tf.squeeze(tf.cast(x, tf.string)))


def model(sess, config):
    if config.debug:
        print('Importing Elmo/USE module...')
    if config.hub.is_set("cache_dir"):
        os.environ['TFHUB_CACHE_DIR'] = config.hub.cache_dir

    embedding_types = {
        "elmo": {
            "name": "elmo",
            "url": "https://tfhub.dev/google/elmo/1",
            "size": 1024
        },
        "use": {
            "name": "use",
            "url": "https://tfhub.dev/google/universal-sentence-encoder/1",
            "size": 512
        }
    }[config.embedding_type]
    elmo_model = hub.Module(embedding_types['url'], trainable=True)

    if config.debug:
        print('Imported.')
    sess.run(tf.global_variables_initializer())
    sess.run(tf.tables_initializer())

    elmo_embeddings = keras.layers.Lambda(ElmoEmbedding(elmo_model, embedding_types), output_shape=(embedding_types['size'],))

    sentence1 = keras.layers.Input(shape=(1,), dtype='string')
    sentence2 = keras.layers.Input(shape=(1,), dtype='string')

    sentence1_emb = elmo_embeddings(sentence1)
    sentence2_emb = elmo_embeddings(sentence2)

    sentences = keras.layers.concatenate([sentence1_emb, sentence2_emb])
    output = keras.layers.Dense(1000, activation='relu')(sentences)
    output = keras.layers.Dropout(0.2)(output)
    output = keras.layers.Dense(500, activation='relu')(output)
    output = keras.layers.Dropout(0.2)(output)
    output = keras.layers.Dense(1, activation='sigmoid')(output)


    # Model
    entailment_model = keras.models.Model(inputs=[sentence1, sentence2], outputs=output)
    entailment_model.compile(optimizer="adam", loss="binary_crossentropy", metrics=['accuracy'])
    return entailment_model


def main(config):
    train_set = SNLIDataloader('data/snli_1.0/snli_1.0_train.jsonl')
    train_set.set_preprocess_fn(preprocess_fn)
    train_set.set_output_fn(output_fn)

    test_set = Dataloader(config, 'data/test_stories.csv', testing_data=True)
    # test_set.set_preprocess_fn(preprocess_fn)
    test_set.load_dataset('data/test.bin')
    test_set.load_vocab('./data/default.voc', config.vocab_size)
    test_set.set_output_fn(output_fn_test)

    generator_training = train_set.get_batch(config.batch_size, config.n_epochs)
    generator_test = test_set.get_batch(config.batch_size, config.n_epochs)


    # Initialize tensorflow session
    sess = tf.Session()
    K.set_session(sess)  # Set to keras backend

    keras_model = model(sess, config)

    verbose = 0 if not config.debug else 1
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Callbacks
    tensorboard = keras.callbacks.TensorBoard(log_dir='./logs/' + timestamp + '-entailmentv3/', histogram_freq=0,
                                              batch_size=config.batch_size,
                                              write_graph=False,
                                              write_grads=True)

    model_path = os.path.abspath(
            os.path.join(os.curdir, './builds/' + timestamp))
    model_path += '-entailmentv3_checkpoint_epoch-{epoch:02d}.hdf5'

    saver = keras.callbacks.ModelCheckpoint(model_path,
                                            monitor='val_loss', verbose=verbose, save_best_only=True)

    keras_model.fit_generator(generator_training, steps_per_epoch=300,
                              epochs=config.n_epochs,
                              verbose=verbose,
                              validation_data=generator_test,
                              validation_steps=len(test_set) / config.batch_size,
                              callbacks=[tensorboard, saver])
