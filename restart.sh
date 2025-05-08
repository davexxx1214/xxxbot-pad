#!/bin/bash
# 删除nohup文件
rm -f nohup.out
# 创建一个新的nohup文件
touch nohup.out
# 强制终止所有python3进程
sudo killall -9 python
# 在后台运行python3 app.py，并将输出重定向到nohup.out，然后跟踪nohup.out文件的输出
nohup python main.py > nohup.out 2>&1 & tail -f nohup.out

