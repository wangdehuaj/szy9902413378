from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
from functools import partial
from collections import Counter, OrderedDict
import pickle
import json
import multiprocessing as mp
import numpy as np
from absl import flags
import tensorflow as tf
from vocabulary import Vocab
from os.path import exists
from os import makedirs
import pickle


class MacOSFile(object):

    def __init__(self, f):
        self.f = f

    def __getattr__(self, item):
        return getattr(self.f, item)

    def read(self, n):
        # print("reading total_bytes=%s" % n, flush=True)
        if n >= (1 << 31):
            buffer = bytearray(n)
            idx = 0
            while idx < n:
                batch_size = min(n - idx, 1 << 31 - 1)
                # print("reading bytes [%s,%s)..." % (idx, idx + batch_size), end="", flush=True)
                buffer[idx:idx + batch_size] = self.f.read(batch_size)
                # print("done.", flush=True)
                idx += batch_size
            return buffer
        return self.f.read(n)

    def write(self, buffer):
        n = len(buffer)
        print("writing total_bytes=%s..." % n, flush=True)
        idx = 0
        while idx < n:
            batch_size = min(n - idx, 1 << 31 - 1)
            print("writing bytes [%s, %s)... " % (idx, idx + batch_size), end="", flush=True)
            self.f.write(buffer[idx:idx + batch_size])
            print("done.", flush=True)
            idx += batch_size


def pickle_dump(obj, file_path):
    with open(file_path, "wb") as f:
        return pickle.dump(obj, MacOSFile(f), protocol=pickle.HIGHEST_PROTOCOL)


def pickle_load(file_path):
    with open(file_path, "rb") as f:
        return pickle.load(MacOSFile(f))


def _int64_feature(values):
    return tf.train.Feature(int64_list=tf.train.Int64List(value=values))


def _float_feature(values):
    return tf.train.Feature(float_list=tf.train.FloatList(value=values))


def batchify(data, batch_size):

    num_step = len(data) // batch_size
    data = data[:batch_size * num_step]
    data = data.reshape(batch_size, num_step)
    return data


def create_ordered_tfrecords(save_dir, basename, data, batch_size, tgt_len):
    file_name = "{}.bsz-{}.tlen-{}.tfrecords".format(basename, batch_size, tgt_len)

    save_path = os.path.join(save_dir, file_name)
    record_writer = tf.python_io.TFRecordWriter(save_path)

    batched_data = batchify(data, batch_size)

    num_batch = 0
    for t in range(0, batched_data.shape[1] - 1, tgt_len):
        cur_tgt_len = min(batched_data.shape[1] - 1 - t, tgt_len)
        if num_batch % 500 == 0:
            print("  processing batch {}".format(num_batch))
        for idx in range(batch_size):
            inputs = batched_data[idx, t:t + cur_tgt_len]
            labels = batched_data[idx, t + 1:t + cur_tgt_len + 1]

            # features dict
            feature = {
                "inputs": _int64_feature(inputs),
                "labels": _int64_feature(labels),
            }

            example = tf.train.Example(features=tf.train.Features(feature=feature))
            record_writer.write(example.SerializeToString())

        num_batch += 1

    record_writer.close()
    print("Done writing {}. batches: {}".format(file_name, num_batch))

    return file_name, num_batch


class Corpus(object):
    def __init__(self, path, dataset, *args, **kwargs):

        self.dataset = dataset
        self.vocab = Vocab(*args, **kwargs)

        self.vocab.count_file(os.path.join(path, "train.txt"))  # ??????vocab????????????counter(?????????????????????????????????????????????)
        self.vocab.count_file(os.path.join(path, "valid.txt"))  # ???????????????????????????

        self.vocab.build_vocab()  # ????????????????????????idx2sym???sym2idx,?????????????????????????????????????????????

        self.train = self.vocab.encode_file(
            os.path.join(path, "train.txt"), ordered=True)
        self.valid = self.vocab.encode_file(
            os.path.join(path, "valid.txt"), ordered=True)

        # self.cutoffs = []  # ??????????????????????????????????????????????????????????????????cutoff??????????????????????????????????????????????????????????????????????????????TPU??????????????????????????????cutoff????????????????????????

    def convert_to_tfrecords(self, split, save_dir, bsz, tgt_len, **kwargs):
        file_names = []

        record_name = "record_info-{}.bsz-{}.tlen-{}.json".format(
                split, bsz, tgt_len)

        record_info_path = os.path.join(save_dir, record_name)
        bin_sizes = None

        file_name, num_batch = create_ordered_tfrecords(
            save_dir, split, getattr(self, split), bsz, tgt_len)

        file_names.append(file_name)

        with open(record_info_path, "w") as fp:
            record_info = {
                "filenames": file_names,
                "bin_sizes": bin_sizes,
                "num_batch": num_batch
            }
            json.dump(record_info, fp)


def get_lm_corpus(data_dir, dataset):

    fn = os.path.join(data_dir, "cache.pkl")
    print(fn)
    if exists(fn):
        print("Loading cached dataset...")
        # with open(fn, "rb") as fp:
        #     corpus = pickle.load(fp)
        # ???OSX??????????????????pickle.load???????????????????????????????????????????????????MacOSFile
        corpus = pickle_load(fn)
    else:
        print("Producing dataset...")
        kwargs = {}
        if FLAGS.use_vocab_file:
            if exists(os.path.join(FLAGS.data_dir, "vocab.txt")):
                kwargs['vocab_file'] = os.path.join(FLAGS.data_dir, "vocab.txt")
            else:
                kwargs['vocab_file'] = None
        kwargs["special"] = ['<unk>', "<eos>"]  # ??????????????????????????????????????????????????????????????????(???????????????????????????)
        kwargs["lower_case"] = False
        # kwargs['max_size'] = Flags.vocab_size # 80000 # ????????????????????????????????????

        corpus = Corpus(data_dir, dataset, **kwargs)

        print("Saving dataset...")
        with open(fn, "wb") as fp:
            #pickle.dump(corpus, fp)  #, protocol=2
            pickle_dump(corpus, fn)
        print("finish Saving dataset...")

        corpus_info = {
            "vocab_size": len(corpus.vocab),
            "dataset": corpus.dataset
        }
        with open(os.path.join(data_dir, "corpus-info.json"), "w") as fp:
            json.dump(corpus_info, fp)

    return corpus


def get_corpus_info(corpus_info_path):
    with open(corpus_info_path, "r") as fp:
        corpus_info = json.load(fp)
    return corpus_info


def load_record_info(record_info_dir, split, per_host_bsz, tgt_len):

    record_name = "record_info-{}.bsz-{}.tlen-{}.json".format(split, per_host_bsz, tgt_len)
    record_info_path = os.path.join(record_info_dir, record_name)
    with open(record_info_path, "r") as fp:
        record_info = json.load(fp)

    return record_info


def get_input_fn(record_info_dir, split, per_host_bsz, tgt_len,
                 num_core_per_host, num_hosts=1):
    """Creates input function."""

    record_info = load_record_info(record_info_dir, split, per_host_bsz, tgt_len)

    file_names = record_info["filenames"]
    num_batch = record_info["num_batch"]

    tf.logging.info("[{}] File names {}".format(split, file_names))

    def input_fn(params):
        # per-core batch size
        per_core_bsz = params["batch_size"]

        # data_dir could be a remote path, e.g., a google storage url
        data_dir = params["data_dir"]

        def parser(record):
            record_spec = {
                "inputs": tf.VarLenFeature(tf.int64),
                "labels": tf.VarLenFeature(tf.int64),
            }

            # retrieve serialized example
            example = tf.parse_single_example(
                serialized=record,
                features=record_spec)

            # cast int64 into int32
            # cast sparse to dense
            for key in list(example.keys()):
                val = example[key]
                if tf.keras.backend.is_sparse(val):
                    # val = tf.sparse.to_dense(val)
                    val = tf.sparse_tensor_to_dense(val)
                if val.dtype == tf.int64:
                    val = tf.to_int32(val)
                example[key] = val
            return example["inputs"], example["labels"]

        file_paths = []  # ???????????????list?????????1???????????????????????????TPU.
        for file_name in file_names:
            file_path = os.path.join(data_dir, file_name)
            file_paths.append(file_path)

        if split == "train":
            dataset = tf.data.Dataset.from_tensor_slices(file_paths)
            if len(file_paths) > 1:
                dataset = dataset.shuffle(len(file_paths)).repeat()
                dataset = tf.data.TFRecordDataset(dataset)
            else:
                # dataset = tf.data.TFRecordDataset(dataset)
                dataset = tf.data.TFRecordDataset(file_paths[0])

            dataset = dataset.map(parser).cache().repeat()
            dataset = dataset.batch(per_core_bsz)
            dataset = dataset.prefetch(num_core_per_host * per_core_bsz)
        else:
            # do not shuffle, repeat or cache in evaluation
            dataset = tf.data.Dataset.from_tensor_slices(file_paths)
            # dataset = tf.data.TFRecordDataset(dataset)
            dataset = tf.data.TFRecordDataset(file_paths[0])
            dataset = dataset.map(parser)
            dataset = dataset.batch(per_core_bsz)
        return dataset

    if split == "train" and num_hosts > 1:  # ????????????
        record_info["num_batch"] = num_batch // num_hosts

    return input_fn, record_info


def get_saved_corpus(data_dir):

    fn = os.path.join(data_dir, "cache.pkl")
    print(fn)
    # assert exists(fn)
    print("Loading cached corpus...")
    # with open(fn, "rb") as fp:
    #     corpus = pickle.load(fp)
    corpus = pickle_load(fn)
    print("finish cached corpus...")
    return corpus


def main(unused_argv):
    del unused_argv  # Unused

    corpus = get_lm_corpus(FLAGS.data_dir, FLAGS.dataset) # data path???dataset name
    save_dir = os.path.join(FLAGS.data_dir, "tfrecords")
    if not exists(save_dir):
        makedirs(save_dir)

    for split, batch_size in zip(
            ["train", "valid"],
            [FLAGS.per_host_train_bsz, FLAGS.per_host_valid_bsz]):

        if batch_size <= 0: continue
        print("Converting {} set...".format(split))
        corpus.convert_to_tfrecords(split, save_dir, batch_size, FLAGS.tgt_len, FLAGS=FLAGS)


if __name__ == "__main__":
    # Data
    DATASET_NAME = 'taobao'  # enwik8  wikitext-103 taobao
    DATA_ROOT = 'data/{}/'.format(DATASET_NAME)  # enwik8  wikitext-103 taobao

    # Training setting
    TGT_LEN = 100
    BSZ = 32

    # vocab setting
    USE_VOCAB_FILE = True
    VOCAB_SZ = 0  # ??????????????????????????????????????????????????????

    FLAGS = flags.FLAGS
    flags.DEFINE_string("data_dir", DATA_ROOT,
                        help="Location of the data corpus")
    flags.DEFINE_string("dataset", default=DATASET_NAME,
                        help="Dataset name.")
    flags.DEFINE_integer("per_host_train_bsz", BSZ,
                         help="train batch size each host")
    flags.DEFINE_integer("per_host_valid_bsz", BSZ,
                         help="valid batch size each host")
    flags.DEFINE_integer("tgt_len", TGT_LEN,
                         help="number of tokens to predict")
    flags.DEFINE_bool("use_vocab_file", default=USE_VOCAB_FILE,
                      help="whether use vocab file")
    flags.DEFINE_integer("vocab_size", VOCAB_SZ,
                         help="vocab size")
    tf.app.run(main)
