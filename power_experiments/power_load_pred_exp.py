import sys
import io
import time
import pickle
import tensorflow as tf
import numpy as np
import scipy.signal as scisig
import matplotlib.pyplot as plt
from IPython.core.debugger import Tracer
debug_here = Tracer()
from power_data_handler import PowerDataHandler, MergePowerHandler
sys.path.insert(0, "../")
import custom_cells as cc
import custom_optimizers as co
from RNN_wrapper import ResidualWrapper
from RNN_wrapper import RnnInputWrapper
from RNN_wrapper import LinearProjWrapper
import eager_STFT as eagerSTFT
import tensorflow.nn.rnn_cell as rnn_cell
from tensorflow.contrib.rnn import LSTMStateTuple
import window_learning as wl


def compute_parameter_total(trainable_variables):
    total_parameters = 0
    for variable in trainable_variables:
        # shape is an array of tf.Dimension
        shape = variable.get_shape()
        # print('var_name', variable.name, 'shape', shape, 'dim', len(shape))
        variable_parameters = 1
        for dim in shape:
            # print(dim)
            variable_parameters *= dim.value
        # print('parameters', variable_parameters)
        total_parameters += variable_parameters
    print('total:', total_parameters)
    return total_parameters


prediction_days = 60
if prediction_days > 1:
    context_days = prediction_days*2
else:
    context_days = 15
base_dir = 'log/power_pred_logs_explore/'
cell_type = 'gru'
num_units = 222
sample_prob = 1.0
init_learning_rate = 0.004
decay_rate = 0.95


epochs = 80
GPUs = [0]
batch_size = 100
# window_function = 'hann'
window_function = 'learned_tukey'
freq_loss = None
use_residuals = True
fft = False
stiefel = False


fifteen_minute_sampling = False
if fifteen_minute_sampling is True:
    decay_steps = 390
else:
    decay_steps = 455

if fifteen_minute_sampling is True:
    samples_per_day = 96
    path = './power_data/15m_by_country_by_company/'
    power_handler = PowerDataHandler(path, context_days)
else:
    samples_per_day = 24
    path = './power_data/15m_by_country_by_company/'
    power_handler_min15 = PowerDataHandler(path, context_days, samples_per_day=96,
                                           test_keys={})
    path = './power_data/30m_by_country_by_company/'
    power_handler_min30 = PowerDataHandler(path, context_days, samples_per_day=48,
                                           test_keys={})
    path = './power_data/1h_by_country_by_company/'
    power_handler_1h = PowerDataHandler(path, context_days, samples_per_day=24,
                                        test_keys={})
    testing_keys = [('germany_TenneT_GER', '2015'),
                    ('germany_Amprion', '2018'),
                    ('austria_CTA', '2017'),
                    ('belgium_CTA', '2016'),
                    ('UK_nationalGrid', '2015')]
    power_handler = MergePowerHandler(context_days, [power_handler_1h,
                                                     power_handler_min30,
                                                     power_handler_min15],
                                      testing_keys=testing_keys)

if prediction_days > 1:
    window_size = int(samples_per_day*4)
    pred_samples = int(prediction_days*samples_per_day)
    discarded_samples = 0
else:
    pred_samples = int(samples_per_day*1.5)
    discarded_samples = int(samples_per_day*0.5)
    window_size = int(samples_per_day)

overlap = int(window_size*0.75)
step_size = window_size - overlap
fft_pred_samples = pred_samples // step_size + 1
input_samples = context_days*samples_per_day

if fft:
    num_proj = int(window_size//2 + 1)
else:
    num_proj = 1

if fft:
    epsilon = 1e-2
else:
    epsilon = None


graph = tf.Graph()
with graph.as_default():

    data_mean = tf.constant(power_handler.mean, tf.float32, name='data_mean')
    data_std = tf.constant(power_handler.std, tf.float32, name='data_std')

    global_step = tf.Variable(0, name='global_step', trainable=False)
    data_nd = tf.placeholder(tf.float32, [batch_size, input_samples, 1])
    data_nd_norm = (data_nd - data_mean)/data_std

    print('data_nd_shape', data_nd.shape)
    dtype = tf.float32
    data_encoder_time, data_decoder_time = tf.split(data_nd_norm,
                                                    [input_samples-pred_samples,
                                                     pred_samples],
                                                    axis=1)
    if fft:
        dtype = tf.complex64
        if window_function == 'learned_gaussian':
            window = wl.gaussian_window(window_size)
        elif window_function == 'learned_plank':
            window = wl.plank_taper(window_size)
        elif window_function == 'learned_tukey':
            window = wl.tukey_window(window_size)
        elif window_function == 'learned_gauss_plank':
            window = wl.gauss_plank_window(window_size)
        else:
            window = scisig.get_window(window_function,
                                       window_size)
            window = tf.constant(window, tf.float32)

        def transpose_stft_squeeze(in_data, window):
            tmp_in_data = tf.transpose(in_data, [0, 2, 1])
            in_data_fft = eagerSTFT.stft(tmp_in_data, window,
                                         window_size, overlap)

            in_data_fft = tf.transpose(in_data_fft, [0, 2, 3, 1])
            idft_shape = in_data_fft.shape.as_list()
            if idft_shape[-1] == 1:
                in_data_fft = tf.squeeze(in_data_fft, axis=-1)
            else:
                in_data_fft = tf.reshape(in_data_fft, [idft_shape[0],
                                                       idft_shape[1],
                                                       -1])
            return in_data_fft, idft_shape
        data_encoder_freq, _ = transpose_stft_squeeze(data_encoder_time,
                                                      window)
        data_decoder_freq, dec_shape = transpose_stft_squeeze(data_decoder_time,
                                                              window)
        fft_pred_samples = data_decoder_freq.shape[1].value

    if cell_type == 'cgRNN':
        if stiefel:
            cell = cc.StiefelGatedRecurrentUnit(num_units, num_proj=num_proj,
                                                complex_input=fft,
                                                complex_output=fft,
                                                activation=cc.mod_relu,
                                                stiefel=stiefel)
        else:
            cell = cc.StiefelGatedRecurrentUnit(num_units, num_proj=num_proj,
                                                complex_input=fft,
                                                complex_output=fft,
                                                activation=cc.hirose,
                                                stiefel=stiefel)
        cell = RnnInputWrapper(1.0, cell)
        if use_residuals:
            cell = ResidualWrapper(cell=cell)
    elif cell_type == 'gru':
        # todo: Add extra dimension, approach.
        gru = rnn_cell.GRUCell(num_units)
        if fft is True:
            dtype = tf.float32
            # concatenate real and imaginary parts.
            data_encoder_freq = tf.concat([tf.real(data_encoder_freq),
                                           tf.imag(data_encoder_freq)],
                                          axis=-1)
            cell = LinearProjWrapper(num_proj*2, cell=gru, sample_prob=sample_prob)
        else:
            cell = LinearProjWrapper(num_proj, cell=gru, sample_prob=sample_prob)
        cell = RnnInputWrapper(1.0, cell)
        if use_residuals:
            cell = ResidualWrapper(cell=cell)
    else:
        print('cell type not supported.')

    if fft:
        encoder_in = data_encoder_freq[:, :-1, :]
        encoder_out_gt = data_encoder_freq[:, 1:, :]
    else:
        encoder_in = data_encoder_time[:, :-1, :]
        encoder_out_gt = data_encoder_time[:, 1:, :]

    with tf.variable_scope("encoder_decoder") as scope:
        zero_state = cell.zero_state(batch_size, dtype=dtype)
        zero_state = LSTMStateTuple(encoder_in[:, 0, :], zero_state[1])
        encoder_out, encoder_state = tf.nn.dynamic_rnn(cell, encoder_in,
                                                       initial_state=zero_state,
                                                       dtype=dtype)
        if not fft:
            decoder_in = tf.zeros([batch_size, pred_samples, 1])
            encoder_state = LSTMStateTuple(data_encoder_time[:, -1, :],
                                           encoder_state[-1])
        else:
            freqs = data_encoder_freq.shape[-1].value
            decoder_in = tf.zeros([batch_size, fft_pred_samples, freqs], dtype=dtype)
            encoder_state = LSTMStateTuple(data_encoder_freq[:, -1, :],
                                           encoder_state[-1])
        cell.close()
        scope.reuse_variables()
        decoder_out, _ = tf.nn.dynamic_rnn(cell, decoder_in,
                                           initial_state=encoder_state,
                                           dtype=dtype)

        if fft and cell_type == 'gru':
            # assemble complex output.
            decoder_freqs_t2 = decoder_out.shape[-1].value
            decoder_out = tf.complex(decoder_out[:, :, :int(decoder_freqs_t2/2)],
                                     decoder_out[:, :, int(decoder_freqs_t2/2):])
            encoder_out = tf.complex(encoder_out[:, :, :int(decoder_freqs_t2/2)],
                                     encoder_out[:, :, :int(decoder_freqs_t2/2)])
            encoder_out_gt = tf.complex(
                encoder_out_gt[:, :, :int(decoder_freqs_t2/2)],
                encoder_out_gt[:, :, :int(decoder_freqs_t2/2)])

            # debug_here()
            # print('hi')

    if fft:
        if (freq_loss == 'complex_abs') or (freq_loss == 'complex_abs_time'):
            diff = data_decoder_freq - decoder_out
            prd_loss = tf.abs(tf.real(diff)) + tf.abs(tf.imag(diff))
            # tf.summary.histogram('complex_abs', prd_loss)
            # tf.summary.histogram('log_complex_abs', tf.log(prd_loss))
            prd_loss = tf.reduce_mean(prd_loss)
            tf.summary.scalar('f_complex_abs', prd_loss)
        if (freq_loss == 'complex_square') or (freq_loss == 'complex_square_time'):
            diff = data_decoder_freq - decoder_out
            prd_loss = tf.real(diff)*tf.real(diff) + tf.imag(diff)*tf.imag(diff)
            # tf.summary.histogram('complex_square', prd_loss)
            prd_loss = tf.reduce_mean(prd_loss)
            tf.summary.scalar('f_complex_square', prd_loss)

        def expand_dims_and_transpose(input_tensor):
            output = tf.expand_dims(input_tensor, -1)
            output = tf.transpose(output, [0, 3, 1, 2])
            return output
        encoder_out = expand_dims_and_transpose(encoder_out)
        decoder_out = expand_dims_and_transpose(decoder_out)
        encoder_out = eagerSTFT.istft(encoder_out, window,
                                      nperseg=window_size,
                                      noverlap=overlap)
        decoder_out = eagerSTFT.istft(decoder_out, window,
                                      nperseg=window_size,
                                      noverlap=overlap,
                                      epsilon=epsilon)
        data_encoder_gt = expand_dims_and_transpose(encoder_out_gt)
        data_decoder_gt = expand_dims_and_transpose(data_decoder_freq)
        data_encoder_gt = eagerSTFT.istft(data_encoder_gt, window,
                                          nperseg=window_size,
                                          noverlap=overlap)
        data_decoder_gt = eagerSTFT.istft(data_decoder_gt, window,
                                          nperseg=window_size,
                                          noverlap=overlap)
        encoder_out = tf.transpose(encoder_out, [0, 2, 1])
        decoder_out = tf.transpose(decoder_out, [0, 2, 1])
        data_encoder_gt = tf.transpose(data_encoder_gt, [0, 2, 1])
        data_decoder_gt = tf.transpose(data_decoder_gt, [0, 2, 1])
    else:
        data_encoder_gt = encoder_out_gt
        data_decoder_gt = data_decoder_time

    # debug_here()
    time_loss = tf.losses.mean_squared_error(
        tf.real(data_decoder_time), tf.real(decoder_out[:, :pred_samples, :]))
    if not fft:
        loss = time_loss
    else:
        if (freq_loss == 'ad_time') or \
           (freq_loss == 'log_mse_time') or \
           (freq_loss == 'mse_time') or \
           (freq_loss == 'log_mse_mse_time') or \
           (freq_loss == 'complex_square_time') or \
           (freq_loss == 'complex_abs_time'):
            print('using freq and time based loss.')
            lambda_t = 1
            loss = prd_loss*lambda_t + time_loss
            tf.summary.scalar('lambda_t', lambda_t)
        elif (freq_loss is None):
            print('time loss only')
            loss = time_loss
        else:
            loss = prd_loss

    # debug_here()
    learning_rate = tf.train.exponential_decay(init_learning_rate, global_step,
                                               decay_steps, decay_rate,
                                               staircase=True)
    tf.summary.scalar('learning_rate', learning_rate)

    if (cell_type == 'orthogonal' or cell_type == 'cgRNN') and (stiefel is True):
        optimizer = co.RMSpropNatGrad(learning_rate, global_step=global_step)
    else:
        optimizer = tf.train.RMSPropOptimizer(learning_rate)
    gvs = optimizer.compute_gradients(loss)

    with tf.variable_scope("clip_grads"):
        capped_gvs = [(tf.clip_by_value(grad, -1., 1.), var) for grad, var in gvs]

    # grad_summary = tf.histogram_summary(grads)
    # training_op = optimizer.minimize(loss, global_step=global_step)
    training_op = optimizer.apply_gradients(capped_gvs, global_step=global_step)
    tf.summary.scalar('time_loss', time_loss)
    tf.summary.scalar('training_loss', loss)

    init_op = tf.global_variables_initializer()
    summary_sum = tf.summary.merge_all()
    total_parameters = compute_parameter_total(tf.trainable_variables())
    saver = tf.train.Saver()

print(total_parameters)
# ipdb.set_trace()
time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
param_str = '_' + cell_type + '_size_' + str(num_units) + \
    '_fft_' + str(fft) + \
    '_fm_' + str(fifteen_minute_sampling) + \
    '_bs_' + str(batch_size) + \
    '_ps_' + str(pred_samples) + \
    '_dis_' + str(discarded_samples) + \
    '_lr_' + str(init_learning_rate) + \
    '_dr_' + str(decay_rate) + \
    '_ds_' + str(decay_steps) + \
    '_sp_' + str(sample_prob) + \
    '_rc_' + str(use_residuals) + \
    '_pt_' + str(total_parameters)

if fft:
    param_str += '_wf_' + str(window_function)
    param_str += '_ws_' + str(window_size)
    param_str += '_ol_' + str(overlap)
    param_str += '_ffts_' + str(step_size)
    param_str += '_fftp_' + str(fft_pred_samples)
    param_str += '_fl_' + str(freq_loss)
    param_str += '_eps_' + str(epsilon)

if stiefel:
    param_str += '_stfl'

print(param_str)
# ipdb.set_trace()
summary_writer = tf.summary.FileWriter(base_dir + time_str + param_str,
                                       graph=graph)
# dump the parameters
with open(base_dir + time_str + param_str + '/param.pkl', 'wb') as file:
    pickle.dump([base_dir, cell_type,
                 num_units, sample_prob, pred_samples, num_proj,
                 init_learning_rate, decay_rate, decay_steps, epochs,
                 GPUs, batch_size, fft,
                 window_function, window_size, overlap,
                 step_size, fft_pred_samples, freq_loss,
                 use_residuals, epsilon], file)


test_data = power_handler.get_test_set()
# train this.
gpu_options = tf.GPUOptions(visible_device_list=str(GPUs)[1:-1])
# gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=1.0)
config = tf.ConfigProto(allow_soft_placement=True,
                        log_device_placement=False,
                        gpu_options=gpu_options)
with tf.Session(graph=graph, config=config) as sess:
    print('initialize....')
    init_op.run()
    for e in range(0, epochs):
        training_batches = power_handler.get_training_set()

        def organize_into_batches(batches):
            batch_total = len(batches)
            split_into = int(batch_total/batch_size)
            batch_lst = np.array_split(np.stack(batches),
                                       split_into)
            return batch_lst

        batch_lst = organize_into_batches(training_batches)
        # ipdb.set_trace()

        for it, batch in enumerate(batch_lst):
            start = time.time()
            # array_split add elements here and there, the true data is at 1
            feed_dict = {data_nd: np.reshape(batch[:batch_size, :, :, 1],
                                             [batch_size, context_days*samples_per_day,
                                              1])}

            np_loss, summary_to_file, np_global_step, _, \
                data_encoder_np, encoder_out_np, data_decoder_np, decoder_out_np, \
                data_nd_np = \
                sess.run([loss, summary_sum, global_step, training_op,
                          data_encoder_gt, encoder_out,
                          data_decoder_gt, decoder_out, data_nd],
                         feed_dict=feed_dict)
            stop = time.time()
            if it % 5 == 0:
                print('it: %5d, loss: %5.6f, time: %1.2f [s], epoch: %3d of %3d'
                      % (it, np_loss, stop-start, e, epochs))

            summary_writer.add_summary(summary_to_file, global_step=np_global_step)

            if it % 100 == 0:
                plt.figure()
                plt.plot(decoder_out_np[0, discarded_samples:pred_samples, 0])
                plt.plot(data_decoder_np[0, discarded_samples:pred_samples, 0])
                plt.plot(np.abs(decoder_out_np[0, discarded_samples:pred_samples, 0]
                                - data_decoder_np[0, discarded_samples:pred_samples, 0]))
                plt.title("Prediction vs. ground truth")
                buf = io.BytesIO()
                plt.savefig(buf, format='png')
                buf.seek(0)
                summary_image = tf.Summary.Image(
                    encoded_image_string=buf.getvalue(),
                    height=int(plt.rcParams["figure.figsize"][0]*100),
                    width=int(plt.rcParams["figure.figsize"][1]*100))
                summary_image = tf.Summary.Value(tag='prediction_error',
                                                 image=summary_image)
                summary_image = tf.Summary(value=[summary_image])
                summary_writer.add_summary(summary_image, global_step=np_global_step)
                plt.close()
                buf.close()

        # epoch done. Save.
        print('Saving a copy.')
        saver.save(sess, base_dir + time_str + param_str + '/weights/cpk',
                   global_step=np_global_step)
        # do a test run.
        print('test run ', end='')
        mse_lst_net = []
        mse_lst_off = []

        test_batch_lst = organize_into_batches(test_data)
        for test_batch in test_batch_lst:
            gt = np.reshape(test_batch[:batch_size, :, :, 1],
                            [batch_size, context_days*samples_per_day, 1])
            official_pred = np.reshape(test_batch[:batch_size, :, :, 0],
                                       [batch_size, context_days*samples_per_day, 1])
            feed_dict = {data_nd: gt}
            if fft:
                np_loss, np_global_step, \
                    data_encoder_np, encoder_out_np, data_decoder_np, decoder_out_np, \
                    data_nd_np, window_np = \
                    sess.run([loss, global_step, data_encoder_gt, encoder_out,
                              data_decoder_gt, decoder_out, data_nd, window],
                             feed_dict=feed_dict)
            else:
                np_loss, np_global_step, \
                    data_encoder_np, encoder_out_np, data_decoder_np, decoder_out_np, \
                    data_nd_np = \
                    sess.run([loss, global_step, data_encoder_gt, encoder_out,
                              data_decoder_gt, decoder_out, data_nd],
                             feed_dict=feed_dict)
            net_pred = decoder_out_np[0, :, 0]*power_handler.std + power_handler.mean
            official_pred = official_pred[0, -pred_samples:, 0]
            gt = gt[0, -pred_samples:, 0]
            mse_lst_net.append(
                np.mean((gt[discarded_samples:]
                         - net_pred[discarded_samples:pred_samples])**2))
            mse_lst_off.append(
                np.mean((gt[discarded_samples:]
                         - official_pred[discarded_samples:pred_samples])**2))
            print('.', end='')
        mse_net = np.mean(np.array(mse_lst_net))
        mse_off = np.mean(np.array(mse_lst_off))
        print()
        print('epoch: %5d,  test mse_net: %5.2f, test mse_off: %5.2f' %
              (e, mse_net, mse_off))
        print('baseline difference: %5.2f' % (mse_off-mse_net))

        # add to tensorboard
        mse_net_summary = tf.Summary.Value(tag='mse_net_test', simple_value=mse_net)
        mse_net_summary = tf.Summary(value=[mse_net_summary])
        mse_off_summary = tf.Summary.Value(tag='mse_off_test', simple_value=mse_off)
        mse_off_summary = tf.Summary(value=[mse_off_summary])
        summary_writer.add_summary(mse_net_summary, global_step=np_global_step)
        summary_writer.add_summary(mse_off_summary, global_step=np_global_step)
        mse_diff_summary = tf.Summary.Value(tag='mse_net_off_diff',
                                            simple_value=mse_off-mse_net)
        mse_diff_summary = tf.Summary(value=[mse_diff_summary])
        summary_writer.add_summary(mse_diff_summary, global_step=np_global_step)
        if fft:
            # window plot in tensorboard.
            plt.figure()
            plt.plot(window_np)
            plt.title(window_function)
            buf2 = io.BytesIO()
            plt.savefig(buf2, format='png')
            buf2.seek(0)
            summary_image2 = tf.Summary.Image(
                encoded_image_string=buf2.getvalue(),
                height=int(plt.rcParams["figure.figsize"][0]*100),
                width=int(plt.rcParams["figure.figsize"][1]*100))
            summary_image2 = tf.Summary.Value(tag=window_function,
                                              image=summary_image2)
            summary_image2 = tf.Summary(value=[summary_image2])
            summary_writer.add_summary(summary_image2, global_step=np_global_step)
            plt.close()
            buf.close()
