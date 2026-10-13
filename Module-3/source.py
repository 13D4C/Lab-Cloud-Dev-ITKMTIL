import hashlib
import shutil
import urllib.request
import zipfile


SOURCE_URL = 'https://aws-tc-largeobjects.s3.us-west-2.amazonaws.com/CUR-TF-200-ACCDEV-2-91558/02-lab-s3/code.zip'
SOURCE_SHA256 = '725d9c85abe0872a4e07480711f3d9abc4673a6c63c53772702bddbde219fe86'


def prepare_source(cache, destination):
    cache.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    archive = cache / 'code.zip'
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCE_SHA256:
        print('Downloading the official Lab 3 website...', flush=True)
        temporary = cache / 'code.zip.part'
        with urllib.request.urlopen(SOURCE_URL, timeout=120) as response, temporary.open('wb') as output:
            shutil.copyfileobj(response, output)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != SOURCE_SHA256:
            raise RuntimeError('The official lab archive checksum does not match.')
        temporary.replace(archive)
    with zipfile.ZipFile(archive) as source:
        for filename in source.namelist():
            if not (destination / filename).resolve().is_relative_to(destination.resolve()):
                raise RuntimeError('Invalid file path in the lab archive.')
        source.extractall(destination)
    return destination / 'resources/website'
