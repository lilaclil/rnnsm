import pickle
import os.path

import numpy as np
import pandas as pd

from keras.models import Sequential, Model
from keras.layers import Dense, Dropout, Activation, Input, Masking, concatenate, Embedding, RepeatVector, Reshape
from keras.layers.recurrent import LSTM
from keras.callbacks import Callback, LambdaCallback, TensorBoard, ReduceLROnPlateau, EarlyStopping, ModelCheckpoint
from keras.optimizers import Adam, RMSprop
from keras import backend as K

from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error
from sklearn.gaussian_process.kernels import Matern
from bayes_opt import BayesianOptimization

import sys
sys.path.insert(0, '../rmtpp')
sys.path.insert(0, '../utils')
from rmtpp_data import *

from plot_format import *
from seaborn import apionly as sns

seed = 42
np.random.seed(seed)

RESULT_PATH = '../../results/rnn/bayes_opt/'

class Rnn:

    def __init__(self, name, run, hidden_neurons=32, n_sessions=100):
        self.predict_sequence = False
        self.hidden_neurons = hidden_neurons
        self.n_sessions = n_sessions
        self.data = RmtppData.instance()
        self.set_x_y(n_sessions=n_sessions)
        self.set_model()
        self.name = name
        self.run = run
        self.best_model_cp_file = '../../results/rnn/{:02d}_{}.hdf5'.format(run, name)
        self.best_model_cp = ModelCheckpoint(self.best_model_cp_file, monitor="val_loss",
                                             save_best_only=True, save_weights_only=False)

    def set_x_y(self, include_churned=False, min_n_sessions=0, n_sessions=100, preset='deltaNextDays'):
        self.x_train_startTime, \
        self.x_test_startTime, \
        self.x_train_startTime_unscaled, \
        self.x_test_startTime_unscaled, \
        self.y_train_startTime, \
        self.y_test_startTime, \
        features, \
        targets = self.data.get_xy(include_churned, min_n_sessions, n_sessions, preset='nextStartUserTimeDays', target_sequences=self.predict_sequence)

        self.x_train, \
        self.x_test, \
        self.x_train_unscaled, \
        self.x_test_unscaled, \
        self.y_train, \
        self.y_test, \
        features, \
        targets = self.data.get_xy(include_churned, min_n_sessions, n_sessions, preset=preset, target_sequences=self.predict_sequence)

        self.features = features
        self.targets = targets
        temporal_features = ['dayOfMonth', 'dayOfWeek', 'hourOfDay', 'deltaPrevDays', 'startUserTimeDays']
        device_index = features.index('device')
        temporal_indices = list(map(features.index, temporal_features))
        behav_indices = list(map(features.index, set(features) - set(temporal_features + ['device'])))

        self.x_train_devices = self.x_train[:,:,device_index]
        self.x_train_temporal = self.x_train[:,:,temporal_indices]
        self.x_train_behav = self.x_train[:,:,behav_indices]
        self.x_test_devices = self.x_test[:,:,device_index]
        self.x_test_temporal = self.x_test[:,:,temporal_indices]
        self.x_test_behav = self.x_test[:,:,behav_indices]

        if self.predict_sequence:
            self.y_train = self.y_train.reshape(self.y_train.shape + (1,))
            self.y_test = self.y_test.reshape(self.y_test.shape + (1,))

        self.y_train = self.y_train.T[1].T.astype('float32')
        self.y_test = self.y_test.T[1].T.astype('float32')

        self.x_train_train, self.x_train_val, self.y_train_train, self.y_train_val = train_test_split(self.x_train, self.y_train, test_size=.2, random_state=42)

    def load_best_weights(self):
        self.model.load_weights(self.best_model_cp_file)

    def set_model(self, lr=0.01):
        self.lr = lr
        len_seq = self.x_train.shape[1]
        n_devices = np.unique(self.x_train_devices).shape[0]
        n_feat = self.x_train.shape[2]
        len_temporal = self.x_train_temporal.shape[2]
        len_behav = self.x_train_behav.shape[2]

        lstm_neurons = self.hidden_neurons

        # use embedding layer for devices
        # device_input = Input(shape=(len_seq,), dtype='int32', name='device_input')
        # device_embedding = Embedding(output_dim=2, input_dim=n_devices,
        #                              input_length=len_seq, mask_zero=True)(device_input)

        # # inputs for temporal and behavioural data
        # temporal_input = Input(shape=(len_seq, len_temporal), name='temporal_input')
        # behav_input = Input(shape=(len_seq, len_behav), name='behav_input')

        # temporal_masking = Masking(mask_value=0.)(temporal_input)
        # behav_masking = Masking(mask_value=0.)(behav_input)

        # merge_inputs = concatenate([device_embedding, temporal_masking, behav_masking])
        inputs = Input(shape=(len_seq, n_feat))
        merge_inputs = Masking(mask_value=0.)(inputs)

        lstm_output = LSTM(lstm_neurons, return_sequences=self.predict_sequence)(merge_inputs)

        predictions = Dense(1, activation='relu', name='predictions')(lstm_output)

        # model = Model(inputs=[device_input, temporal_input, behav_input], outputs=predictions)
        model = Model(inputs, outputs=predictions)

        model.compile(loss='mse', optimizer=RMSprop(lr=lr))

        self.model = model
        return model

    def get_rmse_days(self):
        pred = self.model.predict([self.x_test_devices, self.x_test_temporal, self.x_test_behav])
        rmse_days = np.sqrt(mean_squared_error(self.y_test, pred))
        return rmse_days

    def fit_model(self, initial_epoch=0):
        log_file = '{:02d}_{}_lr{}_inp{}_nsess{}_hiddenNr{}'.format(self.run, self.name, self.lr,self.x_train.shape[2], self.n_sessions, self.hidden_neurons)
        # self.model.fit([self.x_train_devices, self.x_train_temporal, self.x_train_behav], self.y_train, batch_size=1000, epochs=500, validation_split=0.2, verbose=0, initial_epoch=initial_epoch
        # self.model.fit(self.x_train, self.y_train, batch_size=1000, epochs=500, validation_split=0.2, verbose=0, initial_epoch=initial_epoch
        self.model.fit(self.x_train_train, self.y_train_train, batch_size=1000, epochs=500, validation_split=0.2, verbose=0, initial_epoch=initial_epoch
              , callbacks=[
                TensorBoard(log_dir='../../logs/rnn_new/{}'.format(log_file), histogram_freq=100)
                , EarlyStopping(monitor='val_loss', min_delta=0, patience=100, verbose=1, mode='auto')
                # , self.best_model_cp
                ]
             )



def runBayesOpt():
    RESULT_PATH = '../../results/rnn/bayes_opt/'

    bounds = {'hidden_neurons': (1, 100), 'n_sessions': (10,300)}
    n_iter = 25

    bOpt = BayesianOptimization(_evaluatePerformance, bounds)

    bOpt.maximize(init_points=2, n_iter=n_iter, acq='ucb', kappa=5, kernel=Matern())

    with open(RESULT_PATH+'bayes_opt_rnn.pkl', 'wb') as handle:
        pickle.dump(bOpt, handle, protocol=pickle.HIGHEST_PROTOCOL)

    return bOpt

def _evaluatePerformance(hidden_neurons, n_sessions):
    # def __init__(self, name, run, hidden_neurons=32, n_sessions=100):
    K.clear_session()
    hidden_neurons = np.floor(hidden_neurons).astype('int')
    n_sessions = np.floor(n_sessions).astype('int')
    model = Rnn('bayes_opt', 5, hidden_neurons, n_sessions)
    model.fit_model()
    pred = model.model.predict(model.x_train_val)
    mse = mean_squared_error(pred, model.y_train_val)
    return -mse


predPeriod = {
    'start': pd.Timestamp('2016-02-01'),
    'end': pd.Timestamp('2016-06-01')
}
obsPeriod = {
    'start': pd.Timestamp('2015-02-01'),
    'end': pd.Timestamp('2016-02-01')
}
predPeriodHours = (predPeriod['end'] - predPeriod['start']) / np.timedelta64(1, 'h')
hours_year = np.timedelta64(pd.datetime(2017,2,1) - pd.datetime(2016,2,1)) / np.timedelta64(1,'h')

def showResidPlot_short_date(model, y_pred, width=1, height=None):
    startUserTimeDaysCol = model.features.index('startUserTimeDays')
    df = pd.DataFrame()
    df['predicted (days)'] = y_pred
    df['actual (days)'] = model.y_test
    df['daysInObs'] = model.y_test_startTime
    df['date'] = df['daysInObs'] * np.timedelta64(24,'h') + obsPeriod['start']
    df['residual (days)'] = df['predicted (days)'] - df['actual (days)']

    grid = sns.JointGrid('daysInObs', 'residual (days)', data=df, size=figsize(.5,.5)[0], xlim=(0,3000), ylim=(-110,110))
    grid = grid.plot_marginals(sns.distplot, kde=False, color='k')#, shade=True)
    # grid = grid.plot_joint(sns.kdeplot, shade=True, n_levels=12, cmap='Blues', shade_lowest=False, cut=0)
    grid = grid.plot_joint(plt.scatter, alpha=.1, s=6, lw=0)
    # grid = grid.plot_joint(sns.kdeplot, shade=True, n_levels=15, cmap='Blues', shade_lowest=False, cut=0)
    # grid = grid.plot(df.hoursInPred, df['residual (days)'])
    # grid.ax_joint.clear()
    # grid.ax_joint.scatter(df.hoursInPred, df['residual (days)'])
    grid.ax_joint.clear()

    retUnc = grid.ax_joint.scatter(df['daysInObs'], df['residual (days)'], alpha=.1, s=6, lw=0, color='C0', label='Ret. user (uncens.)')

#     obs_cens = ~model.data.split_val_df.observed & ~model.data.split_val_df.churnedFull.astype('bool')
#     retCens = grid.ax_joint.scatter(df.loc[obs_cens, 'hoursInPred'], df.loc[obs_cens, 'residual (days)'], alpha=.1, s=6, lw=0, color='C4', label='Ret. user (cens.)')

    xDates = [pd.datetime(2016,i,1) for i in [2,4,6]]
    xDatesHours = [(d - obsPeriod['start']).to_timedelta64()/np.timedelta64(24,'h') for d in xDates]
    xDatesStr = [d.strftime('%Y-%m') for d in xDates]
    grid.ax_joint.set_xticks(xDatesHours)
    grid.ax_joint.set_xticklabels(xDatesStr)
    grid.ax_joint.set_xlabel('actual return date')
    grid.ax_joint.set_ylabel('residual (days)')
    # grid.ax_joint.legend(handles=[retUnc], loc=1, labelspacing=0.02, handlelength=0.5)
    # grid = grid.plot_joint(plt.scatter)#, shade=True, n_levels=10, cmap='Blues', shade_lowest=False, cut=0)

    grid.ax_joint.set_ylim((-110,110))
    plt.show()


def showResidPlot_short_days(model, y_pred, width=1, height=None):
    startUserTimeDaysCol = model.features.index('startUserTimeDays')
    df = pd.DataFrame()
    df['predicted (days)'] = y_pred
    df['actual (days)'] = model.y_test
    df['daysInObs'] = model.y_test_startTime
    df['date'] = df['daysInObs'] * np.timedelta64(24,'h') + obsPeriod['start']
    df['residual (days)'] = df['predicted (days)'] - df['actual (days)']

    grid = sns.JointGrid('actual (days)', 'residual (days)', data=df, size=figsize(.5,.5)[0], xlim=(0,3000), ylim=(-110,110))
    grid = grid.plot_marginals(sns.distplot, kde=False, color='k')#, shade=True)
    # grid = grid.plot_joint(sns.kdeplot, shade=True, n_levels=12, cmap='Blues', shade_lowest=False, cut=0)
    grid = grid.plot_joint(plt.scatter, alpha=.1, s=6, lw=0)
    # grid = grid.plot_joint(sns.kdeplot, shade=True, n_levels=15, cmap='Blues', shade_lowest=False, cut=0)
    # grid = grid.plot(df.hoursInPred, df['residual (days)'])
    # grid.ax_joint.clear()
    # grid.ax_joint.scatter(df.hoursInPred, df['residual (days)'])
    grid.ax_joint.clear()

    retUnc = grid.ax_joint.scatter(df['actual (days)'], df['residual (days)'], alpha=.1, s=6, lw=0, color='C0', label='Ret. user (uncens.)')

#     obs_cens = ~model.data.split_val_df.observed & ~model.data.split_val_df.churnedFull.astype('bool')
#     retCens = grid.ax_joint.scatter(df.loc[obs_cens, 'hoursInPred'], df.loc[obs_cens, 'residual (days)'], alpha=.1, s=6, lw=0, color='C4', label='Ret. user (cens.)')

    # xDates = [pd.datetime(2016,i,1) for i in [2,4,6]]
    # xDatesHours = [(d - obsPeriod['start']).to_timedelta64()/np.timedelta64(24,'h') for d in xDates]
    # xDatesStr = [d.strftime('%Y-%m') for d in xDates]
    # grid.ax_joint.set_xticks(xDatesHours)
    # grid.ax_joint.set_xticklabels(xDatesStr)
    grid.ax_joint.set_xlabel('actual return time (days)')
    grid.ax_joint.set_ylabel('residual (days)')
    grid.ax_joint.set_ylim((-110,110))
    # grid.ax_joint.legend(handles=[retUnc], loc=1, labelspacing=0.02, handlelength=0.5)
    # grid = grid.plot_joint(plt.scatter)#, shade=True, n_levels=10, cmap='Blues', shade_lowest=False, cut=0)

    plt.show()


