import datetime
import os
import random

import keras
import tensorflow as tf
import tensorflow_hub as hub
import keras.backend as K
import numpy as np
from keras.models import Model
from keras.layers import Input, Dense, Dropout, LeakyReLU, BatchNormalization, Lambda

from utils import Dataloader
from scripts import DefaultScript


class Script(DefaultScript):
    slug = 'alignment'

    def test(self):
        # Initialize tensorflow session
        sess = tf.Session()
        K.set_session(sess)  # Set to keras backend

        if self.config.debug:
            print('Importing Elmo module...')
        if self.config.hub.is_set("cache_dir"):
            os.environ['TFHUB_CACHE_DIR'] = self.config.hub.cache_dir

        elmo_model = hub.Module("https://tfhub.dev/google/elmo/1", trainable=True)
        if self.config.debug:
            print('Imported.')

        sess.run(tf.global_variables_initializer())
        sess.run(tf.tables_initializer())

        self.graph = tf.get_default_graph()

        # elmo_emb_fn = ElmoEmbedding(elmo_model)
        #
        # elmo_embeddings = keras.layers.Lambda(elmo_emb_fn, output_shape=(1024,))
        # sentence = keras.layers.Input(shape=(1,), dtype="string")
        # sentence_emb = elmo_embeddings(sentence)
        #
        # self.elmo_model = keras.models.Model(inputs=sentence, outputs=sentence_emb)

        test_set = Dataloader(self.config, 'data/test_stories.csv', testing_data=True)
        test_set.load_dataset('data/test.bin')
        test_set.load_vocab('./data/default.voc', self.config.vocab_size)
        test_set.set_output_fn(self.output_fn_test)

        generator_test = test_set.get_batch(self.config.batch_size, self.config.n_epochs)

        model = keras.models.load_model(self.config.alignment.final_model)

        self.define_models()

        hits = 0.
        total = 0.
        for inputs, labels in generator_test:
            discr_src, _ = model.predict(inputs, batch_size=self.config.batch_size)
            for k, discr in enumerate(discr_src):
                if discr < 0.5 and labels[0][k] == 0 or discr >= 0.5 and labels[0][k] == 1:  # predicts that the right one is left
                    hits += 1.
                total += 1.
            print(hits/total)

    def train(self):
        import sent2vec
        assert self.config.sent2vec.model is not None, "Please add sent2vec_model config value."
        self.sent2vec_model = sent2vec.Sent2vecModel()
        self.sent2vec_model.load_model(self.config.sent2vec.model)

        # Initialize tensorflow session
        sess = tf.Session()
        K.set_session(sess)  # Set to keras backend

        sess.run(tf.global_variables_initializer())
        sess.run(tf.tables_initializer())

        self.graph = tf.get_default_graph()

        print("Getting datasets")

        # train_set = SNLIDataloaderPairs('data/snli_1.0/snli_1.0_train.jsonl')
        # train_set.set_preprocess_fn(preprocess_fn)
        # train_set.load_vocab('./data/snli_vocab.dat', self.config.vocab_size)
        # train_set.set_output_fn(self.output_fn_snli)

        train_set = Dataloader(self.config, './data/dev_stories.csv', testing_data=True)
        train_set.load_dataset('./data/dev.bin')
        train_set.load_vocab('./data/default.voc', self.config.vocab_size)
        train_set.set_output_fn(self.output_fn)

        test_set = Dataloader(self.config, 'data/test_stories.csv', testing_data=True)
        test_set.load_dataset('data/test.bin')
        test_set.load_vocab('./data/default.voc', self.config.vocab_size)
        test_set.set_output_fn(self.output_fn_test)

        generator_training = train_set.get_batch(self.config.batch_size, self.config.n_epochs)
        generator_dev = test_set.get_batch(self.config.batch_size, self.config.n_epochs)

        self.define_models()

        print("load models")

        model = self.build_graph()
        frozen_model = self.build_frozen_graph()

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        writer = tf.summary.FileWriter('./logs/' + timestamp + '-alignment/', self.graph)

        model_path = os.path.abspath(
                os.path.join(os.curdir, './builds/' + timestamp))
        model_path += '-alignment-model_checkpoint_step-'

        last_created_file = None

        self.use_frozen = True
        min_source_loss = None

        print("beginning training...")

        for epoch in range(self.config.n_epochs):
            self.use_frozen = not self.use_frozen

            for k in range(0, len(train_set), self.config.batch_size):

                inputs, labels = next(generator_training)
                # We train the frozen model and the unfrozen model jointly
                if self.use_frozen:
                    # Generator training
                    metrics = frozen_model.train_on_batch(inputs, labels)
                    if not k % self.config.print_train_every:
                        print_on_tensorboard(writer, frozen_model.metrics_names, metrics,
                                             epoch * len(train_set) + k, 'train_f')
                else:
                    metrics = model.train_on_batch(inputs, labels)
                    if not k % self.config.print_train_every:
                        print_on_tensorboard(writer, model.metrics_names, metrics,
                                             epoch * len(train_set) + k, 'train_uf')

                if not k % self.config.test_and_save_every:
                    test_metrics = []
                    for j in range(0, len(test_set), self.config.batch_size):
                        inputs_val, labels_val = next(generator_dev)
                        if 0 < self.config.limit_test_step <= j:
                            break
                        test_metrics.append(frozen_model.test_on_batch(inputs_val, labels_val))
                    test_metrics = np.mean(test_metrics, axis=0)
                    # Save value to tensorboard
                    print_on_tensorboard(writer, frozen_model.metrics_names, test_metrics,
                                         epoch * len(train_set) + k, 'test')
                    test_metrics_dict = get_dict_from_lists(frozen_model.metrics_names, test_metrics)
                    # We save the model is loss is better for generator
                    # We only want to save the generator model
                    if min_source_loss is None or test_metrics_dict['disrc_src_loss'] < min_source_loss:
                        frozen_model.save(model_path + str(k) + ".hdf5")
                        if last_created_file is not None:
                            os.remove(last_created_file)  # Only keep the best one
                        last_created_file = model_path + str(k) + ".hdf5"

    def define_models(self):
        # Decoder target
        input_target_decoder = Input((4096,))
        layer_1_target_decoder = Dense(2048)
        layer_2_target_decoder = Dense(1024, activation="relu")
        dec_target = EncoderDecoder(layer_1_target_decoder, layer_2_target_decoder)
        self.decoder_target_model = Model(input_target_decoder, dec_target(input_target_decoder))
        self.decoder_target_model.compile(keras.optimizers.Adam(lr=self.config.learning_rate), "binary_crossentropy")

        # Encoder src
        input_src_encoder = Input((2048,))
        layer_1_src_encoder = Dense(2048)
        layer_2_src_encoder = Dense(4096, activation="relu")
        encoder_src = EncoderDecoder(layer_1_src_encoder, layer_2_src_encoder)
        self.encoder_src_model = Model(input_src_encoder, encoder_src(input_src_encoder))
        self.encoder_src_model.compile(keras.optimizers.Adam(lr=self.config.learning_rate), "binary_crossentropy")

        # Decoder src
        input_src_decoder = Input((4096,))
        layer_1_src_decoder = Dense(4096)
        layer_2_src_decoder = Dense(2048, activation="relu")
        decoder_src = EncoderDecoder(layer_1_src_decoder, layer_2_src_decoder)
        self.decoder_src_model = Model(input_src_decoder, decoder_src(input_src_decoder))
        self.decoder_src_model.compile(keras.optimizers.Adam(lr=self.config.learning_rate), "binary_crossentropy")

        # Encoder target
        input_target_encoder = Input((1024,))
        layer_1_target_encoder = Dense(2048)
        layer_2_target_encoder = Dense(4096, activation="relu")
        encoder_target = EncoderDecoder(layer_1_target_encoder, layer_2_target_encoder)
        self.encoder_target_model = Model(input_target_encoder, encoder_target(input_target_encoder))
        self.encoder_target_model.compile(keras.optimizers.Adam(lr=self.config.learning_rate), "binary_crossentropy")

        # Discriminator
        input_discriminator = Input((4096,))
        layer_1_discriminator = Dense(1026, name="discr_layer_1")
        layer_2_discriminator = Dense(512, name="discr_layer2")
        layer_3_discriminator = Dense(1, activation="sigmoid", name="discr_layer3")
        discriminator = EncoderDecoder(layer_1_discriminator, layer_2_discriminator, layer_3_discriminator,
                                       name="discriminator")

        self.discriminator = Model(input_discriminator, discriminator(input_discriminator))
        self.discriminator.compile(keras.optimizers.Adam(lr=self.config.learning_rate), "binary_crossentropy")

    def build_graph(self):
        input_src_ori = Input((1024,))  # src sentence (only last sentence of story)
        input_src_noise_ori = Input((1024,))  # Noise on src sentence
        input_target = Input((1024,))  # Noise on target sentence
        input_target_noise = Input((1024,))  # Noise on target sentence
        history_ref_ori = Input((1024,))  # Target of the story

        input_src = keras.layers.concatenate([input_src_ori, history_ref_ori])
        input_src_noise = keras.layers.concatenate([input_src_noise_ori, history_ref_ori])

        self.encoder_src_model.trainable = True
        self.encoder_target_model.trainable = True
        self.decoder_src_model.trainable = True
        self.decoder_target_model.trainable = True

        # Build graph
        src_aligned = self.encoder_src_model(input_src_noise)
        out_src = self.decoder_src_model(src_aligned)  # Must be equal to input_src
        out_target_from_src = self.decoder_target_model(src_aligned)

        target_aligned = self.encoder_target_model(input_target_noise)
        out_target = self.decoder_target_model(target_aligned)  # Must be equal to input_target
        out_src_from_target = self.decoder_src_model(target_aligned)

        discriminator_src = Lambda(lambda x: x, name="disrc_src")(
                self.discriminator(
                        src_aligned))  # 0 src and from src_enc or target_aligned and from target_enc, 1 otherwise
        discriminator_target = Lambda(lambda x: x, name="disrc_target")(self.discriminator(target_aligned))

        # Calculate differences
        # Encoder decoder source
        diff_out_src_input_src = keras.layers.subtract([out_src, input_src])
        dist_src = keras.layers.dot([diff_out_src_input_src, diff_out_src_input_src], axes=1, name="dist_src")

        # Encoder decoder target
        diff_out_target = keras.layers.subtract([out_target, input_target])
        dist_target = keras.layers.dot([diff_out_target, diff_out_target], axes=1,
                                       name="dist_target")
        # Encoder source Decoder target
        diff_out_target_from_source = keras.layers.subtract([out_target_from_src, input_target])
        dist_target_from_source = keras.layers.dot([diff_out_target_from_source, diff_out_target_from_source], axes=1,
                                                   name="dist_target_from_source")

        # Encoder target Decoder source
        diff_out_src_from_target = keras.layers.subtract([out_src_from_target, out_src_from_target])
        dist_src_from_target = keras.layers.dot([diff_out_src_from_target, diff_out_src_from_target], axes=1,
                                                name="dist_source_from_target")

        total_distance = keras.layers.add([dist_src, dist_target, dist_target_from_source, dist_src_from_target],
                                          name="distances")

        model = Model(inputs=[input_src_ori, input_src_noise_ori, input_target, input_target_noise, history_ref_ori],
                      outputs=[total_distance, discriminator_src, discriminator_target])
        model.compile(keras.optimizers.Adam(lr=self.config.learning_rate), "binary_crossentropy", ['accuracy'])
        return model

    def build_frozen_graph(self):
        input_src_noise_ori = Input((1024,), name="input_src_noise_ori")  # Noise on src sentence
        input_target_noise = Input((1024,), name="input_target_noise")  # Noise on target sentence
        history_ref_ori = Input((1024,), name="history_ref_ori")  # Target of the story

        input_src_noise = keras.layers.concatenate([input_src_noise_ori, history_ref_ori])

        self.encoder_src_model.trainable = False
        self.encoder_target_model.trainable = False
        self.decoder_src_model.trainable = False
        self.decoder_target_model.trainable = False

        # Build graph
        src_aligned = self.encoder_src_model(input_src_noise)

        target_aligned = self.encoder_target_model(input_target_noise)

        discriminator_src = Lambda(lambda x: x, name="disrc_src")(
                self.discriminator(
                        src_aligned))  # 0 src and from src_enc or target_aligned and from target_enc, 1 otherwise
        discriminator_target = Lambda(lambda x: x, name="disrc_target")(self.discriminator(target_aligned))

        model = Model(inputs=[input_src_noise_ori, input_target_noise, history_ref_ori],
                      outputs=[discriminator_src, discriminator_target])
        model.compile(keras.optimizers.Adam(lr=self.config.learning_rate), "binary_crossentropy", ['accuracy'])

        self.encoder_src_model.trainable = True
        self.encoder_target_model.trainable = True
        self.decoder_src_model.trainable = True
        self.decoder_target_model.trainable = True
        return model

    def add_noise(self, variable, drop_probability: float = 0.1, shuffle_max_distance: int = 3):
        """
        :param variable:np array that : [[sentence1][sentence2]]
        :param drop_probability: we drop every word in the input sentence with a probability
        :param shuffle_max_distance: we slightly shuffle the input sentence
        :return:
        """
        variable = np.array([[variable]])

        def perm(i):
            return i[0] + (shuffle_max_distance + 1) * np.random.random()

        sequences = []
        for b in range(variable.shape[0]):
            sequence = variable[b]
            if type(sequence) != list:
                sequence = sequence.tolist()
            sequence, reminder = sequence[:-1], sequence[-1:]
            if len(sequence) != 0:
                counter = 0
                for num, val in enumerate(np.random.random_sample(len(sequence))):
                    if val < drop_probability:
                        sequence.pop(num - counter)
                        counter = counter + 1
                sequence = [x for _, x in sorted(enumerate(sequence), key=perm)]
            sequence = np.concatenate((sequence, reminder), axis=0)
            sequences.append(sequence)
        new_variable = np.array(sequences)
        return new_variable[0, 0]

    def embedding(self, x):
        sentences = []
        for sentence in x:
            sentences.append(self.sent2vec_model.embed_sentence(sentence))
        return np.array(sentences)

    def output_fn_snli(self, _, batch):
        all_stories_beg_embedded = []
        all_stories_end_embedded = []
        all_stories_noise_beg = []
        all_stories_noise_end = []
        all_history_ref = []
        for b in batch:
            story_ref = b[0][0]
            story_beg = b[0][1]
            story_noise_beg = self.add_noise(story_beg)
            story_end = b[1][1]
            story_noise_end = self.add_noise(story_end)
            all_history_ref.append(story_ref)
            if not self.use_frozen:  # We send the real values
                all_stories_end_embedded.append(story_end)
                all_stories_noise_beg.append(story_noise_beg)
                all_stories_noise_end.append(story_noise_end)
                all_stories_beg_embedded.append(story_beg)
            else:  # We mix them up
                all_stories_end_embedded.append(story_beg)
                all_stories_noise_beg.append(story_noise_end)
                all_stories_noise_end.append(story_noise_beg)
                all_stories_beg_embedded.append(story_end)
        all_stories_end_embedded = self.embedding(np.array(all_stories_end_embedded))
        all_stories_beg_embedded = self.embedding(np.array(all_stories_beg_embedded))
        all_stories_noise_end = self.embedding(np.array(all_stories_noise_end))
        all_stories_noise_beg = self.embedding(np.array(all_stories_noise_beg))
        all_history_ref_embedding = self.embedding(np.array(all_history_ref))
        if self.use_frozen:  # We switched up the sources and targets
            ones = np.ones(len(batch))
            # disriminator must be one because inverted
            return [all_stories_beg_embedded, all_stories_noise_beg,
                    all_stories_end_embedded, all_stories_noise_end, all_history_ref_embedding], [ones, ones]
        zeros = np.zeros(len(batch))
        return [all_stories_beg_embedded, all_stories_noise_beg,
                all_stories_end_embedded, all_stories_noise_end, all_history_ref_embedding], [zeros, zeros, zeros]

    def output_fn(self, data):
        """
        :param data:
        :return:
        """
        batch = np.array(data.batch)
        all_stories_beg_embedded = []
        all_stories_end_embedded = []
        all_stories_noise_beg = []
        all_stories_noise_end = []
        all_history_ref = []
        for b in batch:
            all_history_ref.append(" ".join(b[3]))
            story_beg = " ".join(b[4])
            story_noise_beg = self.add_noise(story_beg)
            story_end = " ".join(b[5])
            story_noise_end = self.add_noise(story_end)
            label = int(b[6][0]) - 1
            # sentences inverted and not use frozen or sentence correct and use frozen
            if not self.use_frozen and label == 1 or self.use_frozen and label == 0:
                all_stories_beg_embedded.append(story_end)
                all_stories_end_embedded.append(story_beg)
                all_stories_noise_beg.append(story_noise_end)
                all_stories_noise_end.append(story_noise_beg)
            else:
                all_stories_beg_embedded.append(story_beg)
                all_stories_end_embedded.append(story_end)
                all_stories_noise_beg.append(story_noise_beg)
                all_stories_noise_end.append(story_noise_end)
        all_stories_end_embedded = self.embedding(np.array(all_stories_end_embedded))
        all_stories_beg_embedded = self.embedding(np.array(all_stories_beg_embedded))
        all_stories_noise_end = self.embedding(np.array(all_stories_noise_end))
        all_stories_noise_beg = self.embedding(np.array(all_stories_noise_beg))
        all_history_ref_embedding = self.embedding(np.array(all_history_ref))
        if self.use_frozen:  # We switched up the sources and targets
            ones = np.ones(len(batch))
            # disriminator must be one because inverted
            return [all_stories_noise_beg,
                    all_stories_noise_end, all_history_ref_embedding], [ones, ones]
        zeros = np.zeros(len(batch))
        return [all_stories_beg_embedded, all_stories_noise_beg,
                all_stories_end_embedded, all_stories_noise_end, all_history_ref_embedding], [zeros, zeros, zeros]

    def output_fn_test(self, data):
        """
        :param data:
        :return:
        """
        batch = np.array(data.batch)
        all_stories_beg_embedded = []
        all_stories_end_embedded = []
        all_stories_noise_beg = []
        all_stories_noise_end = []
        all_history_ref = []
        label1 = []
        label2 = []
        for b in batch:
            all_history_ref.append(" ".join(b[3]))
            story_beg = " ".join(b[4])
            story_noise_beg = self.add_noise(story_beg)
            story_end = " ".join(b[5])
            story_noise_end = self.add_noise(story_end)
            all_stories_beg_embedded.append(story_beg)
            all_stories_end_embedded.append(story_end)
            all_stories_noise_beg.append(story_noise_beg)
            all_stories_noise_end.append(story_noise_end)
            label = int(b[6][0]) - 1
            # 0 if beginning = src = true sentence
            # 1 if beginning = target = false sentence
            label1.append(label)
            label2.append(1 - label)
        all_stories_noise_end = self.embedding(np.array(all_stories_noise_end))
        all_stories_noise_beg = self.embedding(np.array(all_stories_noise_beg))
        all_history_ref_embedding = self.embedding(np.array(all_history_ref))
        label1 = np.array(label1)
        label2 = np.array(label2)
        return [all_stories_noise_beg,
                all_stories_noise_end, all_history_ref_embedding], [label1, label2]


# class ElmoEmbedding:
#     def __init__(self, elmo_model):
#         self.elmo_model = elmo_model
#         self.__name__ = "elmo_embeddings"
#
#     def __call__(self, x):
#         return self.elmo_model(tf.squeeze(tf.cast(x, tf.string)), signature="default", as_dict=True)[
#             "default"]


def preprocess_fn(line):
    output = [line['sentence1'], line['sentence2']]
    return output


class EncoderDecoder:
    def __init__(self, layer1, layer2, layer3=None, name=None):
        self.layer2 = layer2
        self.layer1 = layer1
        self.layer3 = layer3
        if name is not None and layer3 is not None:
            self.layer3.name = name
        elif name is not None:
            self.layer2.name = name

    def __call__(self, x):
        l1 = BatchNormalization()(Dropout(0.3)(LeakyReLU()(self.layer1(x))))
        if self.layer3 is not None:
            l2 = BatchNormalization()(Dropout(0.3)(LeakyReLU()(self.layer2(l1))))
            return self.layer3(l2)
        else:
            return self.layer2(l1)


def print_on_tensorboard(writer, metrics, results, k, prefix=""):
    """
    Add values to summary
    :param writer: tensroflow writer
    :param metrics: metric names
    :param results: metric values
    :param k: x axis
    :param prefix: prefix to add the names
    """
    # Save value to tensorboard
    accuracy_summary = tf.Summary()
    for name, value in zip(metrics, results):
        accuracy_summary.value.add(tag=prefix + "_" + name, simple_value=value)
    writer.add_summary(accuracy_summary, k)


def get_dict_from_lists(keys, values):
    """
    Construct a dict from two lists
    :param keys:
    :param values:
    :return:
    """
    result = {}
    for name, value in zip(keys, values):
        result[name] = value
    return result
