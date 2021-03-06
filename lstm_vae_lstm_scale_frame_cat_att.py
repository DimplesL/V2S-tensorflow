#-*- coding: utf-8 -*-
import tensorflow as tf
import pandas as pd
import numpy as np
import os, h5py, sys, argparse, random
import pdb
import time
import json
from collections import defaultdict
#from keras.preprocessing import sequence
from cocoeval import COCOScorer
import unicodedata
from tensorflow.python.tools.inspect_checkpoint import print_tensors_in_checkpoint_file
from modules.variational_autoencoder import VAE
from utils.model_ops_msrvtt import *
from utils.record_helper import read_and_decode_frame_cat_att

###### custom parameters #######
model_path = '/disk_new/shenxu/models/lstm_cat_att/'
cpu_device = "/cpu:0"
learning_rate = 0.001
drop_strategy = 'keep'
caption_weight = 1.
video_weight = 1.
latent_weight = 0.001
test_v2s = True
test_v2v = True
test_s2s = True
test_s2v = True
video_data_path_train = '/home/shenxu/data/msrvtt_frame_cat_att/train.tfrecords'
video_data_path_val = '/home/shenxu/data/msrvtt_frame_cat_att/val.tfrecords'
video_data_path_test = '/home/shenxu/data/msrvtt_frame_cat_att/test.tfrecords'
###### custom parameters #######

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

        with tf.device("/cpu:0"):
            self.Wemb = tf.Variable(tf.random_uniform([n_words, dim_hidden], -0.1, 0.1), name='Wemb')

        # encoding LSTM for video
        self.lstm1 = tf.contrib.rnn.LSTMCell(self.dim_hidden, use_peepholes=True, state_is_tuple=True)
        # encoding LSTM for sentence
        self.lstm2 = tf.contrib.rnn.LSTMCell(self.dim_hidden, use_peepholes=True, state_is_tuple=True)
        # decoding LSTM for sentence
        self.lstm3 = tf.contrib.rnn.LSTMCell(self.dim_hidden, use_peepholes=True, state_is_tuple=True)
        # decoding LSTM for video
        self.lstm4 = tf.contrib.rnn.LSTMCell(self.dim_hidden, use_peepholes=True, state_is_tuple=True)

        self.lstm1_dropout = tf.contrib.rnn.DropoutWrapper(self.lstm1,output_keep_prob=1 - self.drop_out_rate)
        self.lstm2_dropout = tf.contrib.rnn.DropoutWrapper(self.lstm2,output_keep_prob=1 - self.drop_out_rate)
        self.lstm3_dropout = tf.contrib.rnn.DropoutWrapper(self.lstm3,output_keep_prob=1 - self.drop_out_rate)
        self.lstm4_dropout = tf.contrib.rnn.DropoutWrapper(self.lstm4,output_keep_prob=1 - self.drop_out_rate)

        self.vae = VAE(self.dim_hidden * 2, self.dim_hidden)

        self.encode_image_W = tf.Variable(tf.random_uniform([dim_video_feat, dim_hidden], -0.1, 0.1), name='encode_image_W')
        self.encode_image_b = tf.Variable(tf.zeros([dim_hidden]), name='encode_image_b')
        self.decode_image_W = tf.Variable(tf.random_uniform([dim_hidden, dim_image], -0.1, 0.1, name='decode_image_W'))
        self.decode_image_b = tf.Variable(tf.random_uniform([dim_image]), name='decode_image_b')

        self.embed_word_W = tf.Variable(tf.random_uniform([dim_hidden, n_words], -0.1,0.1), name='embed_word_W')
        if bias_init_vector is not None:
            self.embed_word_b = tf.Variable(bias_init_vector.astype(np.float32), name='embed_word_b')
        else:
            self.embed_word_b = tf.Variable(tf.zeros([n_words]), name='embed_word_b')

        # attribute embedding
        self.embed_att_w = tf.Variable(tf.random_uniform([dim_att, dim_hidden], -0.1,0.1), name='embed_att_w')
        self.embed_att_b = tf.Variable(tf.zeros([dim_hidden]), name='embed_att_b')


        # learnable coefficient for normalized video and sentence feature
        self.video_coeff = tf.Variable(tf.ones([1]), name='video_coeff')
        self.sent_coeff = tf.Variable(tf.ones([1]), name='sent_coeff')
        self.h2h_w = tf.Variable(tf.random_uniform([dim_hidden, dim_hidden], -0.1, 0.1), name='h2h_w')
        self.h2h_b = tf.Variable(tf.zeros([dim_hidden]), name='h2h_b')
        self.h2c_w = tf.Variable(tf.random_uniform([dim_hidden, dim_hidden], -0.1, 0.1), name='h2c_w')
        self.h2c_b = tf.Variable(tf.zeros([dim_hidden]), name='h2c_b')

    def build_model(self, frames, encode_video, video_mask, caption, caption_1, caption_mask,
        cat_data, att_data):
        drop_type = tf.placeholder(tf.int32, shape=[])
        video_mask = tf.cast(video_mask, tf.float32)
        caption_mask = tf.cast(caption_mask, tf.float32)
        frames = frames * tf.constant(pixel_scale_factor)

        ### embedding of attribute
        embed_att= tf.nn.xw_plus_b(att_data, self.embed_att_w, self.embed_att_b)

        ### for encoding
        encode_video_flat = tf.reshape(encode_video, [-1, dim_video_feat]) # (b x nv) x d
        encode_image_emb = tf.nn.xw_plus_b(encode_video_flat, self.encode_image_W, self.encode_image_b) # (b x nv) x h
        encode_image_emb = tf.reshape(encode_image_emb, [self.batch_size, self.n_video_steps, self.dim_hidden]) # b x n x h

        c_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        m_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h state1 = (c1, m1)
        state1 = (c_init, m_init)
        state2 = (c_init, m_init)

        ######## Encoding Stage #########
        # encoding video
        with tf.variable_scope("model") as scope:
            for i in xrange(self.n_video_steps):
                if i > 0: scope.reuse_variables()
                with tf.variable_scope("LSTM1"):
                   output1, state1 = self.lstm1_dropout(encode_image_emb[:, i, :], state1) # b x h

        with tf.variable_scope("model") as scope:
        # encoding sentence
            for i in xrange(self.n_caption_steps):
                if i > 0: scope.reuse_variables()
                with tf.variable_scope("LSTM2"):
                    with tf.device("/cpu:0"):
                        current_embed = tf.nn.embedding_lookup(self.Wemb, caption_1[:,i])
                    output2, state2 = self.lstm2_dropout(current_embed, state2) # b x h
        ######## Encoding Stage #########
        tf.summary.histogram('v_h', output1)
        tf.summary.histogram('s_h', output2)

        #### 0: keep both 1: keep video only 2: keep sentence only
        ######## Dropout Stage #########
        if drop_type == 1:
            output2 = tf.constant(0.) * output2
            output2 = tf.stop_gradient(output2)
        elif drop_type == 2:
            output1 = tf.constant(0.) * output1
            output1 = tf.stop_gradient(output1)
        ######## Dropout Stage #########

        ######## Semantic Learning Stage ########
        ##### normalization before concatenation
        output1 = self.video_coeff * tf.nn.l2_normalize(output1, 1)
        output2 = self.sent_coeff * tf.nn.l2_normalize(output2, 1)
        input_state = tf.concat([output1, output2], 1) # b x (2 * h)
        loss_latent, output_semantic = self.vae(input_state)
        h0 = tf.matmul(tf.nn.tanh(output_semantic), self.h2h_w) + self.h2h_b
        c0 = tf.matmul(tf.nn.tanh(output_semantic), self.h2c_w) + self.h2c_b
        tf.summary.histogram('z', output_semantic)
        ######## Semantic Learning Stage ########

        ######## Decoding Stage ##########
        state4 = (c0, h0)
        state3 = (c0, h0)
        current_embed = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        video_prev = frames[:, 0, :]

        loss_caption = 0.0
        loss_video = 0.0

        ## decoding sentence with attention
        with tf.variable_scope("model") as scope:
            # first write semantic into memory
#            with tf.variable_scope("LSTM3"):
#                _, state3 = self.lstm3_dropout(output_semantic, state3)
            for i in xrange(self.n_caption_steps):
                if i > 0: tf.get_variable_scope().reuse_variables()
                with tf.variable_scope("LSTM3"):
                    curr_input = tf.concat([cat_data, embed_att, output_semantic, current_embed], 1)
                    output3, state3 = self.lstm3_dropout(curr_input, state3) # b x h

                labels = tf.expand_dims(caption[:,i], 1) # b x 1
                indices = tf.expand_dims(tf.range(0, self.batch_size, 1), 1) # b x 1
                concated = tf.concat([indices, labels], 1) # b x 2
                onehot_labels = tf.sparse_to_dense(concated, tf.stack([self.batch_size, self.n_words]), 1.0, 0.0) # b x w
                with tf.device(cpu_device):
                    current_embed = tf.nn.embedding_lookup(self.Wemb, caption[:,i])

                logit_words = tf.nn.xw_plus_b(output3, self.embed_word_W, self.embed_word_b) # b x w
                cross_entropy = tf.nn.softmax_cross_entropy_with_logits(logits = logit_words,
                    labels = onehot_labels) # b x 1
                cross_entropy = cross_entropy * caption_mask[:,i] # b x 1
                loss_caption += tf.reduce_sum(cross_entropy) # 1

        ## decoding video with attention
        with tf.variable_scope("model") as scope:
            # first write semantic into memory
            for i in xrange(self.n_video_steps - 1):
                if i > 0: tf.get_variable_scope().reuse_variables()
                with tf.variable_scope("LSTM4"):
                    curr_input = tf.concat([output_semantic, video_prev], 1)
                    output4, state4 = self.lstm4_dropout(curr_input, state4)
                decode_image = tf.nn.xw_plus_b(output4, self.decode_image_W, self.decode_image_b) # b x d_im
                decode_image = tf.nn.sigmoid(decode_image)
                video_prev = frames[:, i+1, :] # b x d_im
                euclid_loss = tf.reduce_sum(tf.square(tf.subtract(decode_image, frames[:,i+1,:])),
                    axis=1, keep_dims=True) # b x 1
                euclid_loss = euclid_loss * video_mask[:, i] # b x 1
                loss_video += tf.reduce_sum(euclid_loss) # 1

        loss_caption = loss_caption / tf.reduce_sum(caption_mask)
        loss_video = loss_video / tf.reduce_sum(video_mask)

        loss = caption_weight * loss_caption + latent_weight * loss_latent + video_weight * loss_video
        return loss, loss_caption, loss_latent, loss_video, drop_type

    def build_v2s_generator(self, encode_video, cat_data, att_data):
        c_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        m_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h state1 = (c1, m1)
        state1 = (c_init, m_init)

        ### embedding of attribute
        embed_att= tf.nn.xw_plus_b(att_data, self.embed_att_w, self.embed_att_b)

        ####### Encoding Video ##########
        ### for encoding
        encode_video_flat = tf.reshape(encode_video, [-1, dim_video_feat]) # (b x nv) x d
        encode_image_emb = tf.nn.xw_plus_b(encode_video_flat, self.encode_image_W, self.encode_image_b) # (b x nv) x h
        encode_image_emb = tf.reshape(encode_image_emb, [self.batch_size, self.n_video_steps, self.dim_hidden]) # b x n x h
        # encoding video
        with tf.variable_scope("model") as scope:
            scope.reuse_variables()
            for i in xrange(self.n_video_steps):
                with tf.variable_scope("LSTM1"):
                   output1, state1 = self.lstm1_dropout(encode_image_emb[:, i, :], state1) # b x h
        ####### Encoding Video ##########

        ####### Semantic Mapping ########
        ##### normalization before concatenation
        output1 = self.video_coeff * tf.nn.l2_normalize(output1, 1)
        output2 = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        input_state = tf.concat([output1, output2], 1) # b x h, b x h
        _, output_semantic = self.vae(input_state)
        ####### Semantic Mapping ########

        ####### Decoding ########
        h0 = tf.matmul(tf.nn.tanh(output_semantic), self.h2h_w) + self.h2h_b
        c0 = tf.matmul(tf.nn.tanh(output_semantic), self.h2c_w) + self.h2c_b
        state3 = (c0, h0) # n x 2 x h
        current_embed = tf.zeros([self.batch_size, self.dim_hidden]) # b x h

        generated_words = []

        with tf.variable_scope("model") as scope:
            scope.reuse_variables()
#            with tf.variable_scope("LSTM3"):
#                _, state3 = self.lstm3_dropout(output_semantic, state3)
            for i in range(self.n_caption_steps):
                with tf.variable_scope("LSTM3") as vs:
                    curr_input = tf.concat([cat_data, embed_att, output_semantic, current_embed], 1)
                    output3, state3 = self.lstm3(curr_input, state3 ) # b x h
                    lstm3_variables = [v for v in tf.global_variables() if v.name.startswith(vs.name)]
                logit_words = tf.nn.xw_plus_b(output3, self.embed_word_W, self.embed_word_b) # b x w
                max_prob_index = tf.argmax(logit_words, 1) # b
                generated_words.append(max_prob_index) # b
                with tf.device(cpu_device):
                    current_embed = tf.nn.embedding_lookup(self.Wemb, max_prob_index)
        ####### Decoding ########

        generated_words = tf.transpose(tf.stack(generated_words)) # n_caption_step x 1
        return generated_words, lstm3_variables

    def build_s2s_generator(self, caption_1, cat_data):
        c_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        m_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        state2 = (c_init, m_init) # 2 x b x h

        ######## Encoding Stage #########
        # encoding sentence
        with tf.variable_scope("model") as scope:
            scope.reuse_variables()
            for i in xrange(self.n_caption_steps):
                with tf.variable_scope("LSTM2") as vs:
                    with tf.device(cpu_device):
                        current_embed = tf.nn.embedding_lookup(self.Wemb, caption_1[:,i]) # b x h
                    output2, state2 = self.lstm2_dropout(current_embed, state2) # b x h
                    lstm2_variables = [v for v in tf.global_variables() if v.name.startswith(vs.name)]
        ######## Encoding Stage #########

        ####### Semantic Mapping ########
        ##### normalization before concatenation
        output2 = self.sent_coeff * tf.nn.l2_normalize(output2, 1)
        output1 = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        input_state = tf.concat([output1, output2], 1) # b x h, b x h
        _, output_semantic = self.vae(input_state)
        ####### Semantic Mapping ########

        ####### Decoding ########
        h0 = tf.matmul(tf.nn.tanh(output_semantic), self.h2h_w) + self.h2h_b
        c0 = tf.matmul(tf.nn.tanh(output_semantic), self.h2c_w) + self.h2c_b
        state3 = (c0, h0) # n x 2 x h
        current_embed = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        embed_att = tf.zeros([self.batch_size, self.dim_hidden]) # b x h

        generated_words = []

        with tf.variable_scope("model") as scope:
            scope.reuse_variables()
#            with tf.variable_scope("LSTM3"):
#                _, state3 = self.lstm3_dropout(output_semantic, state3)
            for i in range(self.n_caption_steps):
                with tf.variable_scope("LSTM3") as vs:
                    curr_input = tf.concat([cat_data, embed_att, output_semantic, current_embed], 1)
                    output3, state3 = self.lstm3(curr_input, state3 ) # b x h
                    lstm3_variables = [v for v in tf.global_variables() if v.name.startswith(vs.name)]
                logit_words = tf.nn.xw_plus_b(output3, self.embed_word_W, self.embed_word_b) # b x w
                max_prob_index = tf.argmax(logit_words, 1) # b
                generated_words.append(max_prob_index) # b
                with tf.device(cpu_device):
                    current_embed = tf.nn.embedding_lookup(self.Wemb, max_prob_index)
        ####### Decoding ########

        generated_words = tf.transpose(tf.stack(generated_words)) # n_caption_step x 1
        return generated_words, lstm2_variables, lstm3_variables

    def build_s2v_generator(self, sent, frames):
        frames = frames * tf.constant(pixel_scale_factor)
        ####### Encoding Sentence ##########
        c_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        m_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        state2 = (c_init, m_init)
        with tf.variable_scope("model") as scope:
            for i in xrange(self.n_caption_steps):
                scope.reuse_variables()
                with tf.variable_scope("LSTM2") as vs:
                    with tf.device(cpu_device):
                        current_embed = tf.nn.embedding_lookup(self.Wemb, sent[:, i])
                    output2, state2 = self.lstm2_dropout(current_embed, state2) # b x h
                    lstm2_variables = [v for v in tf.global_variables() if v.name.startswith(vs.name)]
        ####### Encoding Sentence ##########

        ####### Semantic Mapping ########
        ##### normalization before concatenation
        output2 = self.sent_coeff * tf.nn.l2_normalize(output2, 1)
        output1 = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        input_state = tf.concat([output1, output2], 1) # b x (2 * h)
        _, output_semantic = self.vae(input_state)
        ####### Semantic Mapping ########

        ####### Decoding ########
        h0 = tf.matmul(tf.nn.tanh(output_semantic), self.h2h_w) + self.h2h_b
        c0 = tf.matmul(tf.nn.tanh(output_semantic), self.h2c_w) + self.h2c_b
        state4 = (c0, h0) # n x 2 x h
        frame_prev = frames[:, 0, :]

        generated_images = []

        with tf.variable_scope("model") as scope:
            scope.reuse_variables()
            for i in range(self.n_video_steps - 1):
                with tf.variable_scope("LSTM4") as vs:
                    curr_input = tf.concat([output_semantic, frame_prev], 1)
                    output4, state4 = self.lstm4(curr_input, state4) # b x h
                    lstm4_variables = [v for v in tf.global_variables() if v.name.startswith(vs.name)]
                image_prev = tf.nn.xw_plus_b(output4, self.decode_image_W, self.decode_image_b)
                frame_prev = tf.nn.sigmoid(image_prev)
                generated_images.append(frame_prev) # b x d_im
        ####### Decoding ########
        generated_images = tf.transpose(tf.stack(generated_images), [1, 0, 2]) # b x n_video_step x d_im

        return generated_images, lstm2_variables, lstm4_variables

    def build_v2v_generator(self, encode_video, frames):
        c_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        m_init = tf.zeros([self.batch_size, self.dim_hidden]) # b x h state1 = (c1, m1)
        state1 = (c_init, m_init)
        frames = frames * tf.constant(pixel_scale_factor)
        ######## Encoding Stage #########
        ### for encoding
        encode_video_flat = tf.reshape(encode_video, [-1, dim_video_feat]) # (b x nv) x d
        encode_image_emb = tf.nn.xw_plus_b(encode_video_flat, self.encode_image_W, self.encode_image_b) # (b x nv) x h
        encode_image_emb = tf.reshape(encode_image_emb, [self.batch_size, self.n_video_steps, self.dim_hidden]) # b x n x h
        # encoding video
        with tf.variable_scope("model") as scope:
            scope.reuse_variables()
            for i in xrange(self.n_video_steps):
                with tf.variable_scope("LSTM1"):
                   output1, state1 = self.lstm1_dropout(encode_image_emb[:, i, :], state1) # b x h
        ######## Encoding Stage #########

        ####### Semantic Mapping ########
        ##### normalization before concatenation
        output1 = self.video_coeff * tf.nn.l2_normalize(output1, 1)
        output2 = tf.zeros([self.batch_size, self.dim_hidden]) # b x h
        input_state = tf.concat([output1, output2], 1) # b x (2 * h)
        _, output_semantic = self.vae(input_state)
        ####### Semantic Mapping ########

        ####### Decoding ########
        h0 = tf.matmul(tf.nn.tanh(output_semantic), self.h2h_w) + self.h2h_b
        c0 = tf.matmul(tf.nn.tanh(output_semantic), self.h2c_w) + self.h2c_b
        state4 = (c0, h0) # n x 2 x h
        frame_prev = frames[:, 0, :] # b x d_im

        generated_images = []

        with tf.variable_scope("model") as scope:
            scope.reuse_variables()
            for i in range(self.n_video_steps - 1):
                with tf.variable_scope("LSTM4") as vs:
                    curr_input = tf.concat([output_semantic, frame_prev], 1)
                    output4, state4 = self.lstm4(curr_input, state4) # b x h
                    lstm4_variables = [v for v in tf.global_variables() if v.name.startswith(vs.name)]
                image_prev = tf.nn.xw_plus_b(output4, self.decode_image_W, self.decode_image_b)
                frame_prev = tf.nn.sigmoid(image_prev)
                generated_images.append(frame_prev) # b x d_im
        ####### Decoding ########
        generated_images = tf.transpose(tf.stack(generated_images), [1, 0, 2]) # b x n_video_step x d_im

        return generated_images, lstm4_variables

def train():
    assert os.path.isdir(home_folder)
    assert os.path.isfile(video_data_path_train)
    assert os.path.isfile(video_data_path_val)
    assert os.path.isdir(model_path)
    assert os.path.isfile(wordtoix_file)
    assert os.path.isfile(ixtoword_file)
    print 'load meta data...'
    wordtoix = np.load(wordtoix_file).tolist()
    ixtoword = pd.Series(np.load(ixtoword_file).tolist())
    print 'build model and session...'
    # place shared parameters on the GPU
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
    # preprocessing on the CPU
    with tf.device(cpu_device):
        train_data, train_encode_data, _, _, train_video_label, train_caption_label, train_caption_id, train_caption_id_1, \
            train_frame_data, train_cat_data, train_att_data = read_and_decode_frame_cat_att(video_data_path_train)
        val_data, val_encode_data, val_fname, val_title, val_video_label, val_caption_label, val_caption_id, val_caption_id_1, \
            val_frame_data, val_cat_data, val_att_data = read_and_decode_frame_cat_att(video_data_path_val)
        # random batches
        train_frame_data, train_encode_data, train_video_label, train_caption_label, train_caption_id, train_caption_id_1, \
            train_cat_data, train_att_data = \
            tf.train.shuffle_batch([train_frame_data, train_encode_data, train_video_label, train_caption_label, train_caption_id, train_caption_id_1,
                train_cat_data, train_att_data], batch_size=batch_size, num_threads=num_threads, capacity=prefetch, min_after_dequeue=min_queue_examples)
        val_frame_data, val_encode_data, val_video_label, val_fname, val_caption_id, val_caption_id_1, val_cat_data, val_att_data = \
            tf.train.batch([val_frame_data, val_encode_data, val_video_label, val_fname, val_caption_id, val_caption_id_1, \
                val_cat_data ,val_att_data], batch_size=batch_size, num_threads=1, capacity=2*batch_size)
    # operation on the GPU
    with tf.device("/gpu:0"):
        tf_loss, tf_loss_cap, tf_loss_lat, tf_loss_vid, tf_drop_type = \
            model.build_model(train_frame_data, train_encode_data, train_video_label, train_caption_id, train_caption_id_1, \
                train_caption_label, train_cat_data, train_att_data)
        val_v2s_tf,_ = model.build_v2s_generator(val_encode_data, val_cat_data, val_att_data)
        val_s2s_tf,_,_ = model.build_s2s_generator(val_caption_id_1, val_cat_data)
        val_s2v_tf,_,_ = model.build_s2v_generator(val_caption_id_1, val_frame_data)
        val_v2v_tf,_ = model.build_v2v_generator(val_encode_data, val_frame_data)
    sess = tf.InteractiveSession(config=tf.ConfigProto(allow_soft_placement=True, log_device_placement= False))
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
        ## 1. weight decay
        for var in tf.trainable_variables():
            decay_loss = tf.multiply(tf.nn.l2_loss(var), 0.0004, name='weight_loss')
            tf.add_to_collection('losses', decay_loss)
        tf.add_to_collection('losses', tf_loss)
        tf_total_loss = tf.add_n(tf.get_collection('losses'), name='total_loss')
        ## 2. gradient clip
        optimizer = tf.train.AdamOptimizer(learning_rate)
        gvs = optimizer.compute_gradients(tf_total_loss)
        # when variable is not related to the loss, grad returned as None
        clip_gvs = [(tf.clip_by_norm(grad, clip_norm), var) for grad, var in gvs if grad is not None]
        for grad, var in gvs:
            if grad is not None:
                tf.summary.histogram(var.name + '/grad', grad)
                tf.summary.histogram(var.name + '/data', var)
        train_op = optimizer.apply_gradients(clip_gvs)

    ## initialize variables added for optimizer
    sess.run(tf.variables_initializer(set(tf.global_variables()) - temp))
    # initialize epoch variable in queue reader
    sess.run(tf.local_variables_initializer())
    loss_epoch = 0
    loss_epoch_cap = 0
    loss_epoch_vid = 0
    coord = tf.train.Coordinator()
    threads = tf.train.start_queue_runners(sess=sess, coord=coord)
    ##### add summaries ######
    tf.summary.scalar('loss_vid', tf_loss_vid)
    tf.summary.scalar('loss_lat', tf_loss_lat)
    tf.summary.scalar('loss_caption', tf_loss_cap)
#    for var in tf.trainable_variables():
#        summaries.append(tf.histogram_summary(var.op.name, var))
    summary_op = tf.summary.merge_all()
    # write graph architecture to file
    summary_writer = tf.summary.FileWriter(model_path + 'summary', sess.graph)
    epoch = global_step
    video_label = sess.run(train_video_label)
    for step in xrange(1, n_steps+1):
        tStart = time.time()
        if drop_strategy == 'keep':
            drop_type = 0
        elif drop_strategy == 'block_sentence':
            drop_type = 1
        elif drop_strategy == 'block_video':
            drop_type = 2
        else:
            drop_type = random.randint(0, 3)

        _, loss_val, loss_cap, loss_lat, loss_vid = sess.run(
                [train_op, tf_loss, tf_loss_cap, tf_loss_lat, tf_loss_vid],
                feed_dict={
                    tf_drop_type: drop_type
                    })
        tStop = time.time()
        print "step:", step, " Loss:", loss_val, "loss_cap:", loss_cap*caption_weight, "loss_latent:", loss_lat*latent_weight, "loss_vid:", loss_vid * video_weight
        print "Time Cost:", round(tStop - tStart, 2), "s"
        loss_epoch += loss_val
        loss_epoch_cap += loss_cap
        loss_epoch_vid += loss_vid

        if step % n_epoch_steps == 0:
#        if step % 3 == 0:
            epoch += 1
            loss_epoch /= n_epoch_steps
            loss_epoch_cap /= n_epoch_steps
            loss_epoch_vid /= n_epoch_steps
            with tf.device(cpu_device):
                saver.save(sess, os.path.join(model_path, 'model'), global_step=epoch)
#            print 'z:', z[0, :10]
            print 'epoch:', epoch, 'loss:', loss_epoch, "loss_cap:", loss_epoch_cap, "loss_lat:", loss_lat, "loss_vid:", loss_epoch_vid
            loss_epoch = 0
            loss_epoch_cap = 0
            loss_epoch_vid = 0
            ######### test sentence generation ##########
            n_val_steps = int(n_val_samples / batch_size)
#            n_val_steps = 3
            ### TODO: sometimes COCO test show exceptions in the beginning of training ####
            if test_v2s:
                [pred_sent, gt_sent, id_list, gt_dict, pred_dict, flist] = testing_all(sess, 1, ixtoword, val_v2s_tf, val_fname)
                for i, key in enumerate(pred_dict.keys()):
                    print 'video:', flist[i]
                    for ele in gt_dict[key]:
                        print "GT:  " + ele['caption']
                    print "PD:  " + pred_dict[key][0]['caption']
                    print '-------'
                print '############## video to sentence result #################'
                print 'epoch:', epoch
                [pred_sent, gt_sent, id_list, gt_dict, pred_dict, _] = testing_all(sess, n_val_steps, ixtoword, val_v2s_tf, val_fname)
                scorer = COCOScorer()
                total_score = scorer.score(gt_dict, pred_dict, id_list)
                print '############## video to sentence result #################'

            if test_s2s:
                [pred_sent, gt_sent, id_list, gt_dict, pred_dict, flist] = testing_all(sess, 1, ixtoword, val_s2s_tf, val_fname)
                for i,key in enumerate(pred_dict.keys()):
                    print 'video:', flist[i]
                    for ele in gt_dict[key]:
                        print "GT:  " + ele['caption']
                    print "PD:  " + pred_dict[key][0]['caption']
                    print '-------'
                print '############## sentence to sentence result #################'
                print 'epoch:', epoch
                [pred_sent, gt_sent, id_list, gt_dict, pred_dict, _] = testing_all(sess, n_val_steps, ixtoword, val_s2s_tf, val_fname)
                scorer = COCOScorer()
                total_score = scorer.score(gt_dict, pred_dict, id_list)
                print '############## sentence to sentence result #################'

            ######### test video generation #############
            if test_v2v:
                mse_v2v = test_all_videos(sess, n_val_steps, val_frame_data, val_v2v_tf, val_video_label, pixel_scale_factor)
                print 'epoch', epoch, 'video2video mse:', mse_v2v
            if test_s2v:
                mse_s2v = test_all_videos(sess, n_val_steps, val_frame_data, val_s2v_tf, val_video_label, pixel_scale_factor)
                print 'epoch', epoch, 'caption2video mse:', mse_s2v
            sys.stdout.flush()

            ###### summary ######
            if epoch % 2 == 0:
                summary  = sess.run(summary_op)
                summary_writer.add_summary(summary, epoch)

        sys.stdout.flush()

    coord.request_stop()
    coord.join(threads)
    print "Finally, saving the model ..."
    with tf.device(cpu_device):
        saver.save(sess, os.path.join(model_path, 'model'), global_step=n_epochs)
    tStop_total = time.time()
    print "Total Time Cost:", round(tStop_total - tStart_total,2), "s"
    sess.close()

def test(model_path='models/model-900', video_feat_path=video_feat_path):
    meta_data, train_data, val_data, test_data = get_video_data_jukin(video_data_path_train, video_data_path_val, video_data_path_test)
#    test_data = val_data   # to evaluate on testing data or validation data
    ixtoword = pd.Series(np.load(ixtoword_file).tolist())

    model = Video_Caption_Generator(
            dim_image=dim_image,
            n_words=len(ixtoword),
            dim_hidden=dim_hidden,
            batch_size=batch_size,
            n_lstm_steps=n_frame_step,
            drop_out_rate = 0,
            bias_init_vector=None)

    video_tf, video_mask_tf, caption_tf, lstm3_variables_tf = model.build_generator()
    sess = tf.InteractiveSession(config=tf.ConfigProto(allow_soft_placement=True))

    with tf.device("/cpu:0"):
        saver = tf.train.Saver()
        saver.restore(sess, model_path)

    for ind, row in enumerate(lstm3_variables_tf):
        if ind % 4 == 0:
                assign_op = row.assign(tf.multiply(row,1-0.5))
                sess.run(assign_op)

    [pred_sent, gt_sent, id_list, gt_dict, pred_dict] = testing_all(sess, test_data, ixtoword,video_tf, video_mask_tf, caption_tf)
    #np.savez('Att_result/'+model_path.split('/')[1],gt = gt_sent,pred=pred_sent)
    scorer = COCOScorer()
    total_score = scorer.score(gt_dict, pred_dict, id_list)
    return total_score

if __name__ == '__main__':
    args = parse_args()
    if args.task == 'train':
        train()
    elif args.task == 'test':
        total_score = test(model_path = args.model)
