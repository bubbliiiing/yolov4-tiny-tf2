from functools import partial

import tensorflow as tf
from tensorflow.keras.callbacks import EarlyStopping, TensorBoard
from tensorflow.keras.optimizers import Adam

from nets.yolo import get_train_model, yolo_body
from utils.callbacks import (ExponentDecayScheduler, LossHistory,
                             ModelCheckpoint, WarmUpCosineDecayScheduler)
from utils.dataloader import YoloDatasets
from utils.utils import get_anchors, get_classes
from utils.utils_fit import fit_one_epoch

gpus = tf.config.experimental.list_physical_devices(device_type='GPU')
for gpu in gpus:
    tf.config.experimental.set_memory_growth(gpu, True)
    
if __name__ == "__main__":
    #----------------------------------------------------#
    #   是否使用eager模式训练
    #----------------------------------------------------#
    eager = False
    #--------------------------------------------------------#
    #   训练前一定要修改classes_path，使其对应自己的数据集
    #--------------------------------------------------------#
    classes_path    = 'model_data/voc_classes.txt'
    #---------------------------------------------------------------------#
    #   anchors_path代表先验框对应的txt文件，一般不修改。
    #   anchors_mask用于帮助代码找到对应的先验框，一般不修改。
    #---------------------------------------------------------------------#
    anchors_path    = 'model_data/yolo_anchors.txt'
    anchors_mask    = [[3,4,5], [1,2,3]]
    #----------------------------------------------------------------------------------------------------------------------------#
    #   权值文件请看README，百度网盘下载。数据的预训练权重对不同数据集是通用的，因为特征是通用的。
    #   预训练权重对于99%的情况都必须要用，不用的话权值太过随机，特征提取效果不明显，网络训练的结果也不会好。
    #   训练自己的数据集时提示维度不匹配正常，预测的东西都不一样了自然维度不匹配
    #
    #   如果想要断点续练就将model_path设置成logs文件夹下已经训练的权值文件。 
    #   当model_path = ''的时候不加载整个模型的权值。
    #
    #   此处使用的是整个模型的权重，因此是在train.py进行加载的。
    #   如果想要让模型从0开始训练，则设置model_path = ''，下面的Freeze_Train = Fasle，此时从0开始训练，且没有冻结主干的过程。
    #   一般来讲，从0开始训练效果会很差，因为权值太过随机，特征提取效果不明显。
    #----------------------------------------------------------------------------------------------------------------------------#
    model_path      = 'model_data/yolov4_tiny_weights_coco.h5'
    #------------------------------------------------------#
    #   输入的shape大小，一定要是32的倍数
    #------------------------------------------------------#
    input_shape     = [416, 416]
    #-------------------------------#
    #   所使用的注意力机制的类型
    #   phi = 0为不使用注意力机制
    #   phi = 1为SE
    #   phi = 2为CBAM
    #   phi = 3为ECA
    #-------------------------------#
    phi             = 0
    #------------------------------------------------------#
    #   Yolov4的tricks应用
    #   mosaic 马赛克数据增强 True or False 
    #   实际测试时mosaic数据增强并不稳定，所以默认为False
    #   Cosine_scheduler 余弦退火学习率 True or False
    #   label_smoothing 标签平滑 0.01以下一般 如0.01、0.005
    #------------------------------------------------------#
    mosaic              = False
    Cosine_scheduler    = False
    label_smoothing     = 0

    #----------------------------------------------------#
    #   训练分为两个阶段，分别是冻结阶段和解冻阶段。
    #   显存不足与数据集大小无关，提示显存不足请调小batch_size。
    #   受到BatchNorm层影响，batch_size最小为2，不能为1。
    #----------------------------------------------------#
    #----------------------------------------------------#
    #   冻结阶段训练参数
    #   此时模型的主干被冻结了，特征提取网络不发生改变
    #   占用的显存较小，仅对网络进行微调
    #----------------------------------------------------#
    Init_Epoch          = 0
    Freeze_Epoch        = 50
    Freeze_batch_size   = 32
    Freeze_lr           = 1e-3
    #----------------------------------------------------#
    #   解冻阶段训练参数
    #   此时模型的主干不被冻结了，特征提取网络会发生改变
    #   占用的显存较大，网络所有的参数都会发生改变
    #----------------------------------------------------#
    UnFreeze_Epoch      = 100
    Unfreeze_batch_size = 16
    Unfreeze_lr         = 1e-4
    #------------------------------------------------------#
    #   是否进行冻结训练，默认先冻结主干训练后解冻训练。
    #------------------------------------------------------#
    Freeze_Train        = True
    #------------------------------------------------------#
    #   用于设置是否使用多线程读取数据，0代表关闭多线程
    #   开启后会加快数据读取速度，但是会占用更多内存
    #   keras里开启多线程有些时候速度反而慢了许多
    #   在IO为瓶颈的时候再开启多线程，即GPU运算速度远大于读取图片的速度。
    #   在eager模式为False有效
    #------------------------------------------------------#
    num_workers         = 0
    #----------------------------------------------------#
    #   获得图片路径和标签
    #----------------------------------------------------#
    train_annotation_path   = '2007_train.txt'
    val_annotation_path     = '2007_val.txt'

    #----------------------------------------------------#
    #   获取classes和anchor
    #----------------------------------------------------#
    class_names, num_classes = get_classes(classes_path)
    anchors, num_anchors     = get_anchors(anchors_path)

    #------------------------------------------------------#
    #   创建yolo模型
    #------------------------------------------------------#
    model_body  = yolo_body((None, None, 3), anchors_mask, num_classes, phi = phi)
    if model_path != '':
        #------------------------------------------------------#
        #   载入预训练权重
        #------------------------------------------------------#
        print('Load weights {}.'.format(model_path))
        model_body.load_weights(model_path, by_name=True, skip_mismatch=True)

    if not eager:
        model = get_train_model(model_body, input_shape, num_classes, anchors, anchors_mask, label_smoothing)
    #-------------------------------------------------------------------------------#
    #   训练参数的设置
    #   logging表示tensorboard的保存地址
    #   checkpoint用于设置权值保存的细节，period用于修改多少epoch保存一次
    #   reduce_lr用于设置学习率下降的方式
    #   early_stopping用于设定早停，val_loss多次不下降自动结束训练，表示模型基本收敛
    #-------------------------------------------------------------------------------#
    logging         = TensorBoard(log_dir = 'logs/')
    checkpoint      = ModelCheckpoint('logs/ep{epoch:03d}-loss{loss:.3f}-val_loss{val_loss:.3f}.h5',
                            monitor = 'val_loss', save_weights_only = True, save_best_only = False, period = 1)
    if Cosine_scheduler:
        reduce_lr   = WarmUpCosineDecayScheduler(T_max = 5, eta_min = 1e-5)
    else:
        reduce_lr   = ExponentDecayScheduler(decay_rate = 0.94, verbose = 1)
    early_stopping  = EarlyStopping(monitor='val_loss', min_delta = 0, patience = 10, verbose = 1)
    loss_history    = LossHistory('logs/')

    #---------------------------#
    #   读取数据集对应的txt
    #---------------------------#
    with open(train_annotation_path) as f:
        train_lines = f.readlines()
    with open(val_annotation_path) as f:
        val_lines   = f.readlines()
    num_train   = len(train_lines)
    num_val     = len(val_lines)

    if Freeze_Train:
        freeze_layers = 60
        for i in range(freeze_layers): model_body.layers[i].trainable = False
        print('Freeze the first {} layers of total {} layers.'.format(freeze_layers, len(model_body.layers)))
        
    #------------------------------------------------------#
    #   主干特征提取网络特征通用，冻结训练可以加快训练速度
    #   也可以在训练初期防止权值被破坏。
    #   Init_Epoch为起始世代
    #   Freeze_Epoch为冻结训练的世代
    #   UnFreeze_Epoch总训练世代
    #   提示OOM或者显存不足请调小Batch_size
    #------------------------------------------------------#
    if True:
        batch_size  = Freeze_batch_size
        lr          = Freeze_lr
        start_epoch = Init_Epoch
        end_epoch   = Freeze_Epoch

        epoch_step      = num_train // batch_size
        epoch_step_val  = num_val   // batch_size

        if epoch_step == 0 or epoch_step_val == 0:
            raise ValueError('数据集过小，无法进行训练，请扩充数据集。')

        train_dataloader    = YoloDatasets(train_lines, input_shape, anchors, batch_size, num_classes, anchors_mask, mosaic = mosaic, train = True)
        val_dataloader      = YoloDatasets(val_lines, input_shape, anchors, batch_size, num_classes, anchors_mask, mosaic = False, train = False)

        print('Train on {} samples, val on {} samples, with batch size {}.'.format(num_train, num_val, batch_size))
        if eager:
            gen     = tf.data.Dataset.from_generator(partial(train_dataloader.generate), (tf.float32, tf.float32, tf.float32))
            gen_val = tf.data.Dataset.from_generator(partial(val_dataloader.generate), (tf.float32, tf.float32, tf.float32))

            gen     = gen.shuffle(buffer_size = batch_size).prefetch(buffer_size = batch_size)
            gen_val = gen_val.shuffle(buffer_size = batch_size).prefetch(buffer_size = batch_size)

            if Cosine_scheduler:
                lr_schedule = tf.keras.experimental.CosineDecayRestarts(
                    initial_learning_rate = lr, first_decay_steps = 5 * epoch_step, t_mul = 1.0, alpha = 1e-2)
            else:
                lr_schedule = tf.keras.optimizers.schedules.ExponentialDecay(
                    initial_learning_rate = lr, decay_steps = epoch_step, decay_rate=0.92, staircase=True)
            
            optimizer = tf.keras.optimizers.Adam(learning_rate = lr_schedule)

            for epoch in range(start_epoch, end_epoch):
                fit_one_epoch(model_body, loss_history, optimizer, epoch, epoch_step, epoch_step_val, gen, gen_val, 
                            end_epoch, input_shape, anchors, anchors_mask, num_classes, label_smoothing)

        else:
            model.compile(optimizer=Adam(lr = lr), loss={'yolo_loss': lambda y_true, y_pred: y_pred})

            model.fit_generator(
                generator           = train_dataloader,
                steps_per_epoch     = epoch_step,
                validation_data     = val_dataloader,
                validation_steps    = epoch_step_val,
                epochs              = end_epoch,
                initial_epoch       = start_epoch,
                use_multiprocessing = True if num_workers != 0 else False,
                workers             = num_workers,
                callbacks           = [logging, checkpoint, reduce_lr, early_stopping, loss_history]
            )

    if Freeze_Train:
        for i in range(freeze_layers): model_body.layers[i].trainable = True

    if True:
        batch_size  = Unfreeze_batch_size
        lr          = Unfreeze_lr
        start_epoch = Freeze_Epoch
        end_epoch   = UnFreeze_Epoch

        epoch_step      = num_train // batch_size
        epoch_step_val  = num_val   // batch_size

        if epoch_step == 0 or epoch_step_val == 0:
            raise ValueError('数据集过小，无法进行训练，请扩充数据集。')

        train_dataloader    = YoloDatasets(train_lines, input_shape, anchors, batch_size, num_classes, anchors_mask, mosaic = mosaic, train = True)
        val_dataloader      = YoloDatasets(val_lines, input_shape, anchors, batch_size, num_classes, anchors_mask, mosaic = False, train = False)

        print('Train on {} samples, val on {} samples, with batch size {}.'.format(num_train, num_val, batch_size))
        if eager:
            gen     = tf.data.Dataset.from_generator(partial(train_dataloader.generate), (tf.float32, tf.float32, tf.float32))
            gen_val = tf.data.Dataset.from_generator(partial(val_dataloader.generate), (tf.float32, tf.float32, tf.float32))

            gen     = gen.shuffle(buffer_size = batch_size).prefetch(buffer_size = batch_size)
            gen_val = gen_val.shuffle(buffer_size = batch_size).prefetch(buffer_size = batch_size)

            if Cosine_scheduler:
                lr_schedule = tf.keras.experimental.CosineDecayRestarts(
                    initial_learning_rate = lr, first_decay_steps = 5 * epoch_step, t_mul = 1.0, alpha = 1e-2)
            else:
                lr_schedule = tf.keras.optimizers.schedules.ExponentialDecay(
                    initial_learning_rate = lr, decay_steps = epoch_step, decay_rate=0.92, staircase=True)
            
            optimizer = tf.keras.optimizers.Adam(learning_rate = lr_schedule)

            for epoch in range(start_epoch, end_epoch):
                fit_one_epoch(model_body, loss_history, optimizer, epoch, epoch_step, epoch_step_val, gen, gen_val, 
                            end_epoch, input_shape, anchors, anchors_mask, num_classes, label_smoothing)

        else:
            model.compile(optimizer=Adam(lr = lr), loss={'yolo_loss': lambda y_true, y_pred: y_pred})

            model.fit_generator(
                generator           = train_dataloader,
                steps_per_epoch     = epoch_step,
                validation_data     = val_dataloader,
                validation_steps    = epoch_step_val,
                epochs              = end_epoch,
                initial_epoch       = start_epoch,
                use_multiprocessing = True if num_workers != 0 else False,
                workers             = num_workers,
                callbacks           = [logging, checkpoint, reduce_lr, early_stopping, loss_history]
            )
