import warnings
try:
    from urllib3.exceptions import NotOpenSSLWarning
    warnings.filterwarnings('ignore', category=NotOpenSSLWarning)
except ImportError:
    pass

"""活动复盘 Agent —— 本地运行，使用你自己的大模型额度。"""
__version__ = "0.1.0"
