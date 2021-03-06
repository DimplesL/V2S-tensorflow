#-*- coding: utf-8 -*-
import tensorflow as tf
import pandas as pd
import numpy as np
import os, h5py, sys, argparse
import pdb
import time
import json
from collections import defaultdict
#from keras.preprocessing import sequence
from cocoeval import COCOScorer
import unicodedata
from tensorflow.python.tools.inspect_checkpoint import print_tensors_in_checkpoint_file
from modules.variational_autoencoder import VAE
from utils.model_ops import *
from utils.record_helper import read_and_decode
#### custom parameters #####
model_path = '/home/shenxu/V2S-tensorflow/models/s2s/'
learning_rate = 0.0001
cpu_device = '/cpu:1'
dim_image = 2*4096
test_v2s = False
test_v2v = False
test_s2s = True
test_s2v = False
save_demo_sent_s2s = False
save_demo_sent_v2s = False
save_demo_video_s2v = False
save_demo_video_v2v = False
#### custom parameters #####

class Video_Caption_Generator():
    def __init__(self, dim_image, n_words, dim_hidden, batch_size, n_caption_steps,
        n_video_steps, drop_out_rate, bias_init_vector=None):
        self.dim_image = dim_image
        self.n_words = n_words
        self.dim_hidden = dim_hidden
        self.batch_size = batch_size
        self.n_caption_steps = n_caption_steps
        self.drop_out_rate = drop_out_rate
        self.n_video_steps = n_video_steps

        with tf.device(cpu_device):
            self.Wemb = tf.Variable(tf.random_uniform([n_words, dim_hidden], -0.1, 0.1), name='Wemb')

        # encoding LSTM for sentence
        self.lstm2 = tf.contrib.rnn.LSTMCell(self.dim_hidden, use_peepholes=True, state_is_tuple=True)
        # decoding LSTM for sentence
        self.lstm3 = tf.contrib.rnn.LSTMCell(self.dim_hidden, use_peepholes=True, state_is_tuple=True)
        # decoding LSTM for video
        self.lstm4 = tf.contrib.rnn.LSTMCell(self.dim_hidden, use_peepholes=True, state_is_tuple=True)

        self.lstm2_dropout = tf.contrib.rnn.DropoutWrapper(self.lstm2,output_keep_prob=1 - self.drop_out_rate)
        self.lstm3_dropout = tf.contrib.rnn.DropoutWrapper(self.lstm3,output_keep_prob=1 - self.drop_out_rate)
        self.lstm4_dropout = tf.contrib.rnn.DropoutWrapper(self.lstm4,output_keep_prob=1 - self.drop_out_rate)

        self.vae = VAE(self.dim_hidden * 2, self.dim_hidden)

        self.encode_image_W = tf.Variable(tf.random_uniform([dim_image, dim_hidden], -0.1, 0.1),name='encode_image_W')
        self.encode_image_b = tf.Variable(tf.zeros([dim_hidden]), name='encode_image_b')
        self.decode_image_W = tf.Variable(tf.random_uniform([dim_hidden, dim_image], -0.1, 0.1, name='decode_image_W'))
        self.decode_image_b = tf.Variable(tf.random_uniform([dim_image]), name='decode_image_b')

        self.embed_word_W = tf.Variable(tf.random_uniform([dim_hidden, n_words], -0.1,0.1), name='embed_word_W')
        if bias_init_vector is not None:
            self.embed_word_b = tf.Variable(bias_init_vector.astype(np.float32), name='embed_word_b')
        else:
            self.embed_word_b = tf.Variable(tf.zeros([n_words]), name='embed_word_b')

    def build_model(self, caption, caption_1, caption_mask):
        caption_mask = tf.cast(caption_mask, tf.float32)
        c_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        m_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        state2 = (c_init, m_init) # 2 x b x h
        # encoding sentence
        with tf.variable_scope("model") as scope:
            for i in xrange(self.n_caption_steps):
                if i > 0: scope.reuse_variables()
                with tf.variable_scope("LSTM2"):
                    with tf.device(cpu_device):
                        current_embed = tf.nn.embedding_lookup(self.Wemb, caption_1[:,i]) # b x h
                    output2, state2 = self.lstm2_dropout(current_embed, state2) # b x h
        ######## Encoding Stage #########

        loss_caption = 0.0
        current_embed = tf.zeros([self.batch_size, self.dim_hidden]) # b x h

        ## decoding sentence without attention
        with tf.variable_scope("model") as scope:
            for i in xrange(n_caption_steps):
                if i > 0: scope.reuse_variables()
                with tf.variable_scope("LSTM3"):
                    output3, state2 = self.lstm3_dropout(current_embed, state2) # b x h
                labels = tf.expand_dims(caption[:,i], 1) # b x 1
                indices = tf.expand_dims(tf.range(0, self.batch_size, 1), 1) # b x 1
                concated = tf.concat([indices, labels], 1) # b x 2
                onehot_labels = tf.sparse_to_dense(concated,
                    tf.stack([self.batch_size, self.n_words]), 1.0, 0.0) # b x w
                with tf.device(cpu_device):
                    current_embed = tf.nn.embedding_lookup(self.Wemb, caption[:,i])
                logit_words = tf.nn.xw_plus_b(output3, self.embed_word_W, self.embed_word_b) # b x w
                cross_entropy = tf.nn.softmax_cross_entropy_with_logits(logits = logit_words,
                    labels = onehot_labels) # b x 1
                cross_entropy = cross_entropy * caption_mask[:,i] # b x 1
                loss_caption += tf.reduce_sum(cross_entropy) # 1

        loss = loss_caption / tf.reduce_sum(caption_mask)
        return loss

    def build_s2s_generator(self, caption_1):
        ####### Encoding Video ##########
        c_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        m_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        state2 = (c_init, m_init) # 2 x b x h

        ######## Encoding Stage #########
        # encoding sentence
        with tf.variable_scope("model") as scope:
            scope.reuse_variables()
            for i in xrange(self.n_caption_steps):
                with tf.variable_scope("LSTM2"):
                    with tf.device(cpu_device):
                        current_embed = tf.nn.embedding_lookup(self.Wemb, caption_1[:,i]) # b x h
                    output2, state2 = self.lstm2_dropout(current_embed, state2) # b x h
        ######## Encoding Stage #########

        current_embed = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        generated_words = []

        with tf.variable_scope("model") as scope:
            scope.reuse_variables()
            for i in range(self.n_caption_steps):
                with tf.variable_scope("LSTM3") as vs:
                    output3, state2 = self.lstm3(current_embed, state2 ) # b x h
                    lstm3_variables = [v for v in tf.global_variables() if v.name.startswith(vs.name)]
                logit_words = tf.nn.xw_plus_b(output3, self.embed_word_W, self.embed_word_b) # b x w
                max_prob_index = tf.argmax(logit_words, 1) # b
                generated_words.append(max_prob_index) # b
                with tf.device(cpu_device):
                    current_embed = tf.nn.embedding_lookup(self.Wemb, max_prob_index)
        ####### Decoding ########

        generated_words = tf.transpose(tf.stack(generated_words)) # n_caption_step x 1
        return generated_words, lstm3_variables

def train():
    assert os.path.isdir(home_folder)
    assert os.path.isfile(video_data_path_train)
    assert os.path.isfile(video_data_path_val)
    assert os.path.isdir(model_path)
    print 'load meta data...'
    wordtoix = np.load(home_folder + 'data0/msvd_wordtoix.npy').tolist()
    ixtoword = pd.Series(np.load(home_folder + 'data0/msvd_ixtoword.npy').tolist())
    print 'build model and session...'
    # shared parameters on the GPU
    with tf.device("/gpu:0"):
        model = Video_Caption_Generator(
                dim_image=dim_image,
                n_words=len(wordtoix),
                dim_hidden=dim_hidden,
                batch_size=batch_size,
                n_caption_steps=n_caption_steps,
                n_video_steps=n_video_steps,
                drop_out_rate = 0.5,
                bias_init_vector=None)
    tStart_total = time.time()
    n_epoch_steps = int(n_train_samples / batch_size)
    n_steps = n_epochs * n_epoch_steps
    # preprocess on the CPU
    with tf.device('/cpu:0'):
        train_data, train_encode_data, _, _, train_video_label, train_caption_label, train_caption_id, train_caption_id_1, \
            _, _, _, _ = read_and_decode(video_data_path_train)
        val_data, val_encode_data, val_fname, val_title, val_video_label, val_caption_label, val_caption_id, val_caption_id_1, \
            _, _, _, _ = read_and_decode(video_data_path_val)
        # random batches
        train_data, train_encode_data, train_video_label, train_caption_label, train_caption_id, train_caption_id_1 = \
            tf.train.shuffle_batch([train_data, train_encode_data, train_video_label, train_caption_label, train_caption_id, train_caption_id_1],
                batch_size=batch_size, num_threads=num_threads, capacity=prefetch, min_after_dequeue=min_queue_examples)
        val_data, val_video_label, val_fname, val_caption_label, val_caption_id_1 = \
            tf.train.batch([val_data, val_video_label, val_fname, val_caption_label, val_caption_id_1],
                batch_size=batch_size, num_threads=1, capacity=2* batch_size)
    # graph on the GPU
    with tf.device("/gpu:0"):
        tf_loss = model.build_model(train_caption_id, train_caption_id_1, train_caption_label)
        val_caption_tf, val_lstm3_variables_tf = model.build_sent_generator(val_caption_id_1)

    sess = tf.InteractiveSession(config=tf.ConfigProto(allow_soft_placement=True, log_device_placement=False))
    # check for model file
    with tf.device(cpu_device):
        saver = tf.train.Saver(max_to_keep=100)
    ckpt = tf.train.get_checkpoint_state(model_path)
    global_step = 0
    if ckpt and tf.train.checkpoint_exists(ckpt.model_checkpoint_path):
        print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
        saver.restore(sess, ckpt.model_checkpoint_path)
#        print_tensors_in_checkpoint_file(ckpt.model_checkpoint_path, "", True)
        global_step = get_model_step(ckpt.model_checkpoint_path)
        print 'global_step:', global_step
    else:
        print("Created model with fresh parameters.")
        sess.run(tf.global_variables_initializer())
    temp = set(tf.global_variables())
    # train on the GPU
    with tf.device("/gpu:0"):
#        train_op = tf.train.AdamOptimizer(learning_rate).minimize(tf_loss)
        ## initialize variables added for optimizer
        optimizer = tf.train.AdamOptimizer(learning_rate)
        gvs = optimizer.compute_gradients(tf_loss)
        # when variable is not related to the loss, grad returned as None
        clip_gvs = [(tf.clip_by_norm(grad, clip_norm), var) for grad, var in gvs if grad is not None]
        train_op = optimizer.apply_gradients(gvs)

    sess.run(tf.variables_initializer(set(tf.global_variables()) - temp))
    # initialize epoch variable in queue reader
    sess.run(tf.local_variables_initializer())
    loss_epoch = 0
    coord = tf.train.Coordinator()
    threads = tf.train.start_queue_runners(sess=sess, coord=coord)
    # write graph architecture to file
    summary_writer = tf.summary.FileWriter(model_path + 'summary', sess.graph)
    loss_summary = tf.summary.scalar('loss', tf_loss)
    epoch = global_step
    for step in xrange(1, n_steps+1):
        tStart = time.time()
        _, loss_val = sess.run([train_op, tf_loss])
        tStop = time.time()
        print "step:", step, " Loss:", loss_val
        print "Time Cost:", round(tStop - tStart, 2), "s"
        loss_epoch += loss_val

        if step % n_epoch_steps == 0:
            epoch += 1
            loss_epoch /= n_epoch_steps
            with tf.device(cpu_device):
                saver.save(sess, os.path.join(model_path, 'model'), global_step=epoch)
#            print 'z:', z[0, :10]
            print 'epoch:', epoch, 'loss:', loss_epoch
            loss_epoch = 0
            n_val_steps = int(n_val_samples / batch_size)
            ######### test sentence generation ##########
            [pred_sent, gt_sent, id_list, gt_dict, pred_dict, _] = testing_all(sess, 1, ixtoword, val_caption_tf, val_fname)
            for key in pred_dict.keys():
                for ele in gt_dict[key]:
                    print "GT:  " + ele['caption']
                print "PD:  " + pred_dict[key][0]['caption']
                print '-------'
            [pred_sent, gt_sent, id_list, gt_dict, pred_dict, _] = testing_all(sess, n_val_steps, ixtoword, val_caption_tf, val_fname)
            scorer = COCOScorer()
            total_score = scorer.score(gt_dict, pred_dict, id_list)

            #### summary #####
            summary = sess.run(loss_summary)
            summary_writer.add_summary(summary, epoch)
            sys.stdout.flush()

        sys.stdout.flush()

    coord.request_stop()
    coord.join(threads)
    print "Finally, saving the model ..."
    with tf.device(cpu_device):
        saver.save(sess, os.path.join(model_path, 'model'), global_step=n_epochs)
    tStop_total = time.time()
    print "Total Time Cost:", round(tStop_total - tStart_total,2), "s"
    sess.close()

def test(model_path=None,
    video_data_path_test=video_data_path_val,
    n_test_samples=n_val_samples,
    video_name=None):
#    test_data = val_data   # to evaluate on testing data or validation data
    wordtoix = np.load(wordtoix_file).tolist()
    ixtoword = pd.Series(np.load(ixtoword_file).tolist())
    with tf.device("/gpu:0"):
        model = Video_Caption_Generator(
                dim_image=dim_image,
                n_words=len(wordtoix),
                dim_hidden=dim_hidden,
                batch_size=batch_size,
                n_caption_steps=n_caption_steps,
                n_video_steps=n_video_steps,
                drop_out_rate = 0.5,
                bias_init_vector=None)

    # preprocess on the CPU
    with tf.device('/cpu:0'):
        train_data, train_encode_data, _, _, train_video_label, train_caption_label, train_caption_id, train_caption_id_1, \
            _, _, _, _ = read_and_decode(video_data_path_train)
        val_data, val_encode_data, val_fname, val_title, val_video_label, val_caption_label, val_caption_id, val_caption_id_1, \
            _, _, _, _ = read_and_decode(video_data_path_test)
        train_data, train_encode_data, train_video_label, train_caption_label, train_caption_id, train_caption_id_1 = \
            tf.train.shuffle_batch([train_data, train_encode_data, train_video_label, train_caption_label, train_caption_id, train_caption_id_1],
                batch_size=batch_size, num_threads=num_threads, capacity=prefetch, min_after_dequeue=min_queue_examples)
        val_data, val_video_label, val_fname, val_caption_label, val_caption_id_1 = \
            tf.train.batch([val_data, val_video_label, val_fname, val_caption_label, val_caption_id_1],
                batch_size=batch_size, num_threads=1, capacity=2* batch_size)
    # graph on the GPU
    with tf.device("/gpu:0"):
        tf_loss = model.build_model(train_caption_id, train_caption_id_1, train_caption_label)
        val_s2s_tf, s2s_lstm3_vars_tf = model.build_s2s_generator(val_caption_id_1)
    sess = tf.InteractiveSession(config=tf.ConfigProto(allow_soft_placement=True))

    with tf.device(cpu_device):
        saver = tf.train.Saver()
        saver.restore(sess, model_path)
        print 'load parameters from:', model_path

    coord = tf.train.Coordinator()
    threads = tf.train.start_queue_runners(sess=sess, coord=coord)
    ######### test sentence generation ##########
    print 'testing...'
    n_test_steps = int(n_test_samples / batch_size)
    print 'n_test_steps:', n_test_steps
    tstart = time.time()
    ### TODO: sometimes COCO test show exceptions in the beginning of training ####
    if test_s2s:
#        [pred_sent, gt_sent, id_list, gt_dict, pred_dict, flist] = testing_all(sess, 1, ixtoword, val_s2s_tf, val_fname)
#        for i, key in enumerate(pred_dict.keys()):
#            print 'video:', flist[i]
#            for ele in gt_dict[key]:
#                print "GT:  " + ele['caption']
#            print "PD:  " + pred_dict[key][0]['caption']
#            print '-------'
        print '############## sentence to sentence result #################'
        [pred_sent, gt_sent, id_list, gt_dict, pred_dict, flist] = testing_all(sess, n_test_steps, ixtoword, val_s2s_tf, val_fname)
        if os.path.isfile('demo_s2s.txt.videos'):
            video_name = pickle.load(open('demo_s2s.txt.videos', "rb"))
        if video_name:
            for i, key in enumerate(pred_dict.keys()):
                if flist[i] in video_name:
                    print flist[i]
                    for ele in gt_dict[key]:
                        print "GT:  " + ele['caption']
                    print "PD:  " + pred_dict[key][0]['caption']
                    print '-----------'
        scorer = COCOScorer()
        total_score_2 = scorer.score(gt_dict, pred_dict, id_list)
        print '############## sentence to sentence result #################'

    if save_demo_sent_s2s:
        get_demo_sentence(sess, n_test_steps, ixtoword, val_s2s_tf, val_fname, result_file='demo_s2s.txt')

    sys.stdout.flush()
    coord.request_stop()
    coord.join(threads)
    tstop = time.time()
    print "Total Time Cost:", round(tstop - tstart, 2), "s"
    sess.close()

if __name__ == '__main__':
    args = parse_args()
    if args.task == 'train':
        train()
    elif args.task == 'test':
        with tf.device('/gpu:'+str(args.gpu_id)):
            total_score = test(model_path = args.model)
