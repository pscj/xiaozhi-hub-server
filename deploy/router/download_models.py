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

DEFAULT_ENDPOINT = 'https://huggingface.co'
MIRROR_ENDPOINT = 'https://hf-mirror.com'


def download_file(endpoint, filename, partial):
    url = f'{endpoint}/csukuangfj2/{NAME}/resolve/main/{filename}'
    command = ['curl', '-fL', '--retry', '5', '--retry-max-time', '300',
               '--connect-timeout', '30', '--speed-limit', '1024', '--speed-time', '60']
    print(f'Downloading {filename} from {endpoint}', flush=True)
    result = subprocess.run(command + ['-C', '-', '-o', str(partial), url])
    # Some endpoints do not support Range requests. Retry with a fresh file.
    if result.returncode == 33:
        partial.unlink(missing_ok=True)
        result = subprocess.run(command + ['-o', str(partial), url])
    result.check_returncode()


def main():
    root = Path(os.environ.get('HUB_SERVER_DIR', '/opt/xiaozhi-hub/main/xiaozhi-server'))
    target = root / 'models' / NAME
    target.mkdir(parents=True, exist_ok=True)
    endpoint = (os.environ.get('HF_ENDPOINT') or DEFAULT_ENDPOINT).rstrip('/')
    endpoints = [endpoint]
    if endpoint == DEFAULT_ENDPOINT:
        endpoints.append(MIRROR_ENDPOINT)
    for filename, minimum in FILES.items():
        path = target / filename
        if path.is_file() and path.stat().st_size >= minimum:
            continue
        partial = path.with_name(path.name + '.part')
        for index, source in enumerate(endpoints):
            try:
                download_file(source, filename, partial)
                if not partial.is_file() or partial.stat().st_size < minimum:
                    raise RuntimeError(f'Model file is incomplete: {filename}')
            except (subprocess.CalledProcessError, RuntimeError) as error:
                print(f'Download failed: {error}', flush=True)
                if index + 1 == len(endpoints):
                    raise RuntimeError(
                        f'Cannot download {filename} from {", ".join(endpoints)}. '
                        'Check network access or set HF_ENDPOINT to an accessible '
                        'Hugging Face endpoint and rebuild.'
                    ) from error
                # Never append bytes from another source to a partial download.
                partial.unlink(missing_ok=True)
                print(f'Trying fallback endpoint: {endpoints[index + 1]}', flush=True)
            else:
                partial.replace(path)
                break

if __name__ == '__main__':
    main()
