import argparse
from pathlib import Path
from .server import DATA, serve

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='本地单次采集与筛选')
    parser.add_argument('--port', type=int, default=18765)
    parser.add_argument('--data-dir', type=Path, default=DATA)
    args = parser.parse_args()
    serve(port=args.port, data=args.data_dir)
