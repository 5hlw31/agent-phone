"""Shared file policy for tools and HTTP routes. Symlinks are never followed."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import uuid

MAX_FILE_BYTES = 10 * 1024 * 1024
PROTECTED = {'.git', '.ssh', '.aws', '.venv', 'venv', 'node_modules', '__pycache__'}


class Workspace:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def relative(self, path=''):
        if not isinstance(path, str) or '\x00' in path:
            raise ValueError('路径格式不正确')
        candidate = Path(path) if path else Path('.')
        if candidate.is_absolute():
            try:
                candidate = candidate.relative_to(self.root)
            except ValueError:
                raise PermissionError('只能访问工作区内的文件')
        parts = candidate.parts
        if any(part == '..' or part in PROTECTED or part == '.env' or part.startswith('.env.')
               or part.endswith(('.pem', '.key')) for part in parts):
            raise PermissionError('该路径不在可访问范围内')
        return parts

    @contextmanager
    def parent(self, path, create=False):
        parts = self.relative(path)
        if not parts:
            raise PermissionError('不能修改工作区根目录')
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(self.root, flags)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor, parts[-1]
        except OSError as exc:
            if exc.errno in (20, 40, 62):
                raise PermissionError('路径包含符号链接或不是目录') from exc
            raise
        finally:
            os.close(descriptor)

    def path(self, path=''):
        parts = self.relative(path)
        current = self.root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise PermissionError('不允许跟随符号链接')
        return current

    def open_text(self, path):
        with self.parent(path) as (parent, name):
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except OSError as exc:
                if exc.errno in (40, 62):
                    raise PermissionError('不允许跟随符号链接') from exc
                raise
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise PermissionError('只支持普通文件')
                if info.st_nlink > 1:
                    raise PermissionError('不读取存在其他硬链接的文件')
                if info.st_size > MAX_FILE_BYTES:
                    raise ValueError('文件超过 10 MB 限制')
                return os.fdopen(descriptor, 'r', encoding='utf-8', errors='replace')
            except BaseException:
                os.close(descriptor)
                raise

    def read_bytes(self, path):
        with self.open_text(path) as file:
            data = file.buffer.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError('文件超过 10 MB 限制')
        return data

    def read_text(self, path):
        return self.read_bytes(path).decode('utf-8', errors='replace')

    def write_bytes(self, path, content):
        if len(content) > MAX_FILE_BYTES:
            raise ValueError('文件超过 10 MB 限制')
        with self.parent(path, create=True) as (parent, name):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info and not stat.S_ISREG(info.st_mode):
                raise PermissionError('目标不是普通文件')
            temporary = f'.agent-write-{uuid.uuid4().hex}'
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, 'wb') as file:
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                if info:
                    os.chmod(temporary, stat.S_IMODE(info.st_mode), dir_fd=parent)
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def write_text(self, path, content):
        self.write_bytes(path, content.encode('utf-8'))

    def mkdir(self, path):
        with self.parent(path.rstrip('/'), create=True) as (parent, name):
            try:
                os.mkdir(name, 0o755, dir_fd=parent)
            except FileExistsError:
                if not stat.S_ISDIR(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                    raise PermissionError('目标不是目录')

    def delete(self, path):
        with self.parent(path) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                os.rmdir(name, dir_fd=parent)
            elif stat.S_ISREG(info.st_mode):
                os.unlink(name, dir_fd=parent)
            else:
                raise PermissionError('只支持普通文件和空目录')

    def listing(self, path=''):
        root = self.path(path)
        if not root.is_dir():
            raise NotADirectoryError('不是目录')
        entries = []
        for file in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            try:
                self.path(str(file))
                info = file.stat(follow_symlinks=False)
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    continue
                entries.append({'name': file.name, 'path': file.relative_to(self.root).as_posix(),
                                'is_dir': file.is_dir(), 'size': info.st_size, 'mtime': int(info.st_mtime)})
            except (PermissionError, OSError, ValueError):
                continue
        return entries[:1000]
