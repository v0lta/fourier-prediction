# import os
import csv
import random
import numpy as np
import matplotlib.pyplot as plt

from IPython.core.debugger import Tracer
debug_here = Tracer()


class AirDataHandler(object):
    '''
    Data handler meant to load and preproces
    power load data from:
    https://transparency.entsoe.eu/
    '''

    def __init__(self, path_in,
                 path_gt, batch_size=10, sequence_length=168):
        '''
        Creates a power data handler.
        '''
        self.path_in = path_in
        self.path_gt = path_gt
        self._batch_size = batch_size
        self._sequence_length = sequence_length

        def read_file(current_path):
            with open(current_path, newline='') as csvfile:
                data_lst = []
                date_lst = []
                air_reader = csv.reader(csvfile, delimiter=',')
                for i, row in enumerate(air_reader):
                    if i == 0:
                        assert len(row) == 37
                    else:
                        col_lst = []
                        for j, row_el in enumerate(row):
                            if j != 0:
                                try:
                                    col_lst.append(int(row_el))
                                except ValueError:
                                    col_lst.append(np.NaN)
                        data_lst.append(col_lst)
                        date_lst.append(row[0])
            return data_lst, date_lst
        data_lst, date_lst = read_file(path_in)
        data_lst_gt, date_lst_gt = read_file(path_gt)
        # both files must contain data from the same days.
        assert date_lst_gt == date_lst

        # split validation data from the rest.
        def split_val(data_lst_in, date_lst_in):
            date_lst_tr = []
            date_lst_val = []
            data_lst_tr = []
            data_lst_val = []

            for pos, date in enumerate(date_lst_in):
                date_parts = date.split('/')
                val = False
                if date_parts[1] in ('03', '06', '09', '12'):
                    date_lst_val.append(date)
                    data_lst_val.append(data_lst_in[pos])
                    val = True
                if val is False:
                    date_lst_tr.append(date)
                    data_lst_tr.append(data_lst_in[pos])
            return data_lst_val, data_lst_tr, \
                date_lst_val, date_lst_tr

        data_lst_val, data_lst_tr, date_lst_val, date_lst_tr = \
            split_val(data_lst, date_lst)

        data_lst_gt_val, data_lst_gt_tr, date_lst_gt_val, date_lst_gt_tr = \
            split_val(data_lst_gt, date_lst_gt)

        self.data_array = np.array(data_lst_tr)
        self.data_array_gt = np.array(data_lst_gt_tr)
        self.data_array_val = np.array(data_lst_val)
        self.data_array_gt_val = np.array(data_lst_gt_val)
        self.mean = np.nanmean(self.data_array, axis=0)
        self.std = np.nanstd(self.data_array, axis=0)
        self.norm_data = (self.data_array - self.mean)/self.std
        self.norm_data_gt = (self.data_array_gt - self.mean)/self.std
        self.norm_data_val = (self.data_array_val - self.mean)/self.std
        self.norm_data_gt_val = (self.data_array_gt_val - self.mean)/self.std

    def get_epoch(self):
        '''
        Partition the normalized data array into a list
        of batches.
        '''
        data_shape = self.norm_data.shape
        batch_no = int(data_shape[0]/self._sequence_length)
        select = batch_no*self._sequence_length

        def batch(data_array, batch_no, select):
            to_batches = data_array[:select, :]
            batched_data = np.split(to_batches, batch_no, 0)
            batched_data = np.stack(batched_data, 0)
            return batched_data

        batch_data = batch(self.norm_data, batch_no, select)
        batch_data_gt = batch(self.norm_data_gt, batch_no, select)
        idx = list(range(batch_no))
        random.shuffle(idx)
        return_data = np.zeros(batch_data.shape)
        return_data_gt = np.zeros(batch_data_gt.shape)
        for i, j in enumerate(idx):
            return_data[i, :, :] = batch_data[j, :, :]
            return_data_gt[i, :, :] = batch_data_gt[j, :, :]

        baches_per_epoch = batch_no//self._batch_size
        cut = baches_per_epoch*self._batch_size
        creturn_data = return_data[:cut, :, :]
        creturn_data_gt = return_data_gt[:cut, :, :]

        return_lst = np.split(creturn_data, baches_per_epoch, 0)
        return_lst_gt = np.split(creturn_data_gt, baches_per_epoch, 0)
        return return_lst, return_lst_gt

    def get_validation_data(self):
        debug_here()
        return np.expand_dims(np.expand_dims(self.norm_data_val, 0), -1), \
            np.expand_dims(np.expand_dims(self.norm_data_gt_val, 0), -1)


if __name__ == "__main__":
    from scipy import signal
    path_gt = './SampleData/pm25_ground.txt'
    path_in = './SampleData/pm25_missing.txt'
    air_handler = AirDataHandler(path_in, path_gt, batch_size=10,
                                 sequence_length=168)
    epoch = air_handler.get_epoch()

    plt.imshow(epoch[0][0][0])
    plt.show()

    flat = air_handler.norm_data.flatten()
    mm5 = [0 if np.isnan(x) else x for x in flat]
    mm5 = np.reshape(mm5, air_handler.norm_data.shape)
    # train_set = power_handler.get_training_set()
    # plt.plot(np.abs(spectrum).transpose())

    b, a = signal.butter(4, 0.6, 'low')
    filter_gt = signal.filtfilt(b, a, mm5, axis=0)
    # filter_gt = np.fft.irfft(spectrum)
    # plt.subplot(2, 1, 1)
    # plt.subplot(2, 1, 2)
    plt.plot(mm5[:, 1])
    plt.plot(filter_gt[:, 1])
    plt.show()

    spectrum = np.fft.rfft(mm5.transpose())
    spectrum_filt = np.fft.rfft(filter_gt.transpose())
    plt.plot(np.abs(spectrum).transpose()[:, 0])
    plt.plot(np.abs(spectrum_filt).transpose()[:, 0])
    plt.show()

    # compute an STFT.
    f, t, Zxx = signal.stft(mm5.transpose(), nperseg=256)
    Zxx_abs = np.abs(Zxx)
    Zxx_split = np.split(Zxx_abs, Zxx_abs.shape[0], 0)
    Zxx_split = [np.squeeze(Zxx_el) for Zxx_el in Zxx_split]
    Zxx_conc = np.concatenate(Zxx_split, -1)
