import glob
import os
import sys
import time
from pathlib import Path
from loguru import logger as logger_instance

def init_loguru(logger_instance, logger_name, log_level: str="DEBUG"):
    logger_instance.level(log_level)

    # 获取到项目Base目录
    log_path = get_base_path()
    #delete_log_files(logger_instance, log_path)
    init_loguru_files(logger_instance, logger_name, log_path, log_level)

def get_base_path():
    # 获取到项目Base目录
    project_path = Path.cwd()
    log_path = Path(project_path, "logs")

    return log_path

# 删除现有的日志文件
def delete_log_files(logger_instance, directory):
    # 使用glob模块找到目录下的所有日志文件
    log_files = glob.glob(os.path.join(directory, '*.log'))
    print("ready to delete the log files")

    # 遍历所有日志文件并删除
    for log_file in log_files:
        try:
            os.remove(log_file)
        except OSError as e:
            print(f"Error deleting {log_file}: {e}")

def init_loguru_files(logger_instance, logger_name:str, log_path: str, log_level: str="DEBUG"):
    # 添加新的日志处理器
    time_str = time.strftime("%Y-%m-%d")
    logger_instance.info("logging log path:{}", log_path)
    # # 重定向标准输出和错误
    # sys.stdout = StreamToLogger("INFO")
    # sys.stderr = StreamToLogger("ERROR")

    logger_instance.add(f"{log_path}/{logger_name}_{time_str}.log", rotation="500MB", encoding="utf-8", enqueue=True, retention="6")
    logger_instance.add(f"{log_path}/{logger_name}_{time_str}_debug.log", rotation="500MB", encoding="utf-8", enqueue=True, retention="6", level="DEBUG")
    logger_instance.add(f"{log_path}/{logger_name}_{time_str}_info.log", rotation="200MB", encoding="utf-8", enqueue=True, retention="6", level="INFO")
    logger_instance.add(f"{log_path}/{logger_name}_{time_str}_error.log", rotation="100MB", encoding="utf-8", enqueue=True, retention="5 days", level="ERROR")
