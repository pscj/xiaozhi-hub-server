"""Download the same default X-ASR files as scripts/install.sh, during image build."""
import os
from pathlib import Path
import subprocess

NAME = 'sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03'
FILES = {
    'encoder-epoch-99-avg-1.int8.onnx': 150_000_000,
    'decoder-epoch-99-avg-1.onnx': 10_000_000,
    'joiner-epoch-99-avg-1.int8.onnx': 2_000_000,
    'tokens.txt': 50_000,
    'bpe.model': 100_000,
}

def main():
    root = Path(os.environ.get('HUB_SERVER_DIR', '/opt/xiaozhi-hub/main/xiaozhi-server'))
    target = root / 'models' / NAME
    target.mkdir(parents=True, exist_ok=True)
    endpoint = os.environ.get('HF_ENDPOINT', 'https://huggingface.co').rstrip('/')
    for filename, minimum in FILES.items():
        path = target / filename
        if path.is_file() and path.stat().st_size >= minimum:
            continue
        partial = path.with_name(path.name + '.part')
        subprocess.run(['curl', '-fL', '--retry', '3', '-C', '-', '-o', str(partial),
                        f'{endpoint}/csukuangfj2/{NAME}/resolve/main/{filename}'], check=True)
        if partial.stat().st_size < minimum:
            raise RuntimeError(f'Model file is incomplete: {filename}')
        partial.replace(path)

if __name__ == '__main__':
    main()
