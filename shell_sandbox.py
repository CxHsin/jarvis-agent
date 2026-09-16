"""Fail-closed shell execution. Windows processes run in an AppContainer job."""
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import uuid


class SandboxUnavailable(OSError):
    pass


def _verify_tree(root, protected, check):
    """Do not propagate a capability through reparse points or hardlinked files."""
    pending = [root]
    while pending:
        check()
        path = pending.pop()
        if path == protected or path.is_relative_to(protected):
            continue
        info = path.lstat()
        if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise SandboxUnavailable(f'Shell workspace/runtime contains a reparse point: {path}')
        if path.is_dir():
            pending.extend(path.iterdir())
        elif info.st_nlink > 1:
            raise SandboxUnavailable(f'Shell workspace/runtime contains a hardlinked file: {path}')


def start_shell(command, workspace, protected, output, check=lambda: None):
    if os.name != 'nt':
        raise SandboxUnavailable('Shell isolation requires Windows AppContainer on this platform; unsandboxed fallback is disabled')
    return WindowsShell(command, Path(workspace).resolve(), Path(protected).resolve(), output, check)


class WindowsShell:
    """A suspended launch assigned to a kill-on-close job before any user code runs."""
    def __init__(self, command, workspace, protected, output, check):
        import ctypes as c
        from ctypes import wintypes as w
        import msvcrt
        self.c = c
        self.check = check
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.userenv = c.WinDLL('userenv', use_last_error=True)
        self.advapi = c.WinDLL('advapi32', use_last_error=True)
        self.name = 'Jarvis.Shell.' + uuid.uuid4().hex
        self.sid = c.c_void_p()
        self.profile_created = False
        self.granted = []
        self.security_handles = {}
        self.process = self.thread = self.job = None
        self.mutex = None
        self.mutex_owned = False
        self.returncode = None
        self.temp = tempfile.TemporaryDirectory(prefix='jarvis-shell-')
        self.protected = protected
        self.readonly = {Path(__file__).resolve().parent, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()}
        self._fds = []
        self.attrs = None

        def api(dll, name, args, result=w.BOOL):
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result
            return fn
        self.close_handle = api(self.kernel, 'CloseHandle', [w.HANDLE])
        self.free_sid = api(self.advapi, 'FreeSid', [c.c_void_p], c.c_void_p)
        self.delete_profile = api(self.userenv, 'DeleteAppContainerProfile', [w.LPCWSTR], c.c_long)
        self.exit_code = api(self.kernel, 'GetExitCodeProcess', [w.HANDLE, c.POINTER(w.DWORD)])
        self.wait_for = api(self.kernel, 'WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD)
        self.kill_job = api(self.kernel, 'TerminateJobObject', [w.HANDLE, w.UINT])
        self.delete_attrs = api(self.kernel, 'DeleteProcThreadAttributeList', [c.c_void_p], None)
        self.local_free = api(self.kernel, 'LocalFree', [c.c_void_p], c.c_void_p)
        self.release_mutex = api(self.kernel, 'ReleaseMutex', [w.HANDLE])

        class Startup(c.Structure):
            _fields_ = [('cb', w.DWORD), ('lpReserved', w.LPWSTR), ('lpDesktop', w.LPWSTR),
                ('lpTitle', w.LPWSTR), ('dwX', w.DWORD), ('dwY', w.DWORD),
                ('dwXSize', w.DWORD), ('dwYSize', w.DWORD), ('dwXCountChars', w.DWORD),
                ('dwYCountChars', w.DWORD), ('dwFillAttribute', w.DWORD), ('dwFlags', w.DWORD),
                ('wShowWindow', w.WORD), ('cbReserved2', w.WORD), ('lpReserved2', c.c_void_p),
                ('hStdInput', w.HANDLE), ('hStdOutput', w.HANDLE), ('hStdError', w.HANDLE)]
        class StartupEx(c.Structure):
            _fields_ = [('startup', Startup), ('attributes', c.c_void_p)]
        class ProcessInfo(c.Structure):
            _fields_ = [('process', w.HANDLE), ('thread', w.HANDLE), ('pid', w.DWORD), ('tid', w.DWORD)]
        class Capabilities(c.Structure):
            _fields_ = [('sid', c.c_void_p), ('capabilities', c.c_void_p), ('count', w.DWORD), ('reserved', w.DWORD)]
        class BasicLimits(c.Structure):
            _fields_ = [('processTime', c.c_int64), ('jobTime', c.c_int64), ('flags', w.DWORD),
                ('minWorking', c.c_size_t), ('maxWorking', c.c_size_t), ('activeLimit', w.DWORD),
                ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IoCounters(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ('readOps', 'writeOps', 'otherOps', 'readBytes', 'writeBytes', 'otherBytes')]
        class JobLimits(c.Structure):
            _fields_ = [('basic', BasicLimits), ('io', IoCounters), ('processMemory', c.c_size_t),
                ('jobMemory', c.c_size_t), ('peakProcess', c.c_size_t), ('peakJob', c.c_size_t)]

        try:
            create_mutex = api(self.kernel, 'CreateMutexW', [c.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE)
            self.mutex = create_mutex(None, False, 'Local\\Jarvis.Shell.FilesystemCapabilities')
            self._check(self.mutex)
            waiting_since = time.monotonic()
            while self.wait_for(self.mutex, 100) not in (0, 0x80):
                check()
                if time.monotonic() - waiting_since >= 30:
                    raise SandboxUnavailable('Timed out waiting for shell isolation setup')
            self.mutex_owned = True
            if workspace == protected or workspace.is_relative_to(protected):
                raise SandboxUnavailable('Workspace must not be inside the protected state directory')
            protected.mkdir(parents=True, exist_ok=True)
            for path in {workspace, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()}:
                _verify_tree(path, protected, check)
            create_profile = api(self.userenv, 'CreateAppContainerProfile',
                [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, c.c_void_p, w.DWORD, c.POINTER(c.c_void_p)], c.c_long)
            hr = create_profile(self.name, self.name, 'Isolated Jarvis shell', None, 0, c.byref(self.sid))
            if hr != 0:
                raise SandboxUnavailable(f'CreateAppContainerProfile failed: 0x{hr & 0xffffffff:08x}')
            self.profile_created = True
            string_sid = w.LPWSTR()
            convert_sid = api(self.advapi, 'ConvertSidToStringSidW', [c.c_void_p, c.POINTER(w.LPWSTR)])
            self._check(convert_sid(self.sid, c.byref(string_sid)))
            self.sid_text = string_sid.value
            self.local_free(c.cast(string_sid, c.c_void_p))
            for path in [protected, *protected.rglob('*')]:
                check()
                if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise SandboxUnavailable('Protected state contains a reparse point')
                self._set_acl(path, 0, 0)
            # Grant only the workspace, scratch and the running Python runtime.
            # Deny the complete state tree (including memory and session control data).
            self._acl(protected, 'deny', 'F')
            for path in {Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()}:
                self._allow_tree(path, 'RX')
            self._allow_tree(workspace, 'M')
            self._acl(Path(self.temp.name), 'grant', 'M')

            create_job = api(self.kernel, 'CreateJobObjectW', [c.c_void_p, w.LPCWSTR], w.HANDLE)
            self.job = create_job(None, None)
            self._check(self.job)
            limits = JobLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
            set_job = api(self.kernel, 'SetInformationJobObject', [w.HANDLE, c.c_int, c.c_void_p, w.DWORD])
            self._check(set_job(self.job, 9, c.byref(limits), c.sizeof(limits)))

            initialize = api(self.kernel, 'InitializeProcThreadAttributeList', [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)])
            update = api(self.kernel, 'UpdateProcThreadAttribute', [c.c_void_p, w.DWORD, c.c_size_t, c.c_void_p, c.c_size_t, c.c_void_p, c.c_void_p])
            size = c.c_size_t()
            initialize(None, 2, 0, c.byref(size))
            self.attrs = c.create_string_buffer(size.value)
            self._check(initialize(self.attrs, 2, 0, c.byref(size)))
            capabilities = Capabilities(self.sid, None, 0, 0)
            self._check(update(self.attrs, 0, 0x20009, c.byref(capabilities), c.sizeof(capabilities), None, None))
            for fd in (os.open(os.devnull, os.O_RDONLY), os.dup(output.fileno())):
                self._fds.append(fd)
                os.set_inheritable(fd, True)
            handles = (w.HANDLE * 2)(*(msvcrt.get_osfhandle(fd) for fd in self._fds))
            self._check(update(self.attrs, 0, 0x20002, handles, c.sizeof(handles), None, None))
            startup = StartupEx()
            startup.startup.cb = c.sizeof(startup)
            startup.startup.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.hStdInput = handles[0]
            startup.startup.hStdOutput = startup.startup.hStdError = handles[1]
            startup.attributes = c.cast(self.attrs, c.c_void_p)
            info = ProcessInfo()
            system = Path(os.environ['SystemRoot']) / 'System32'
            executable = str(system / 'cmd.exe')
            env = dict(SystemRoot=os.environ['SystemRoot'], WINDIR=os.environ['SystemRoot'],
                COMSPEC=executable, PATH=os.pathsep.join([str(system), str(Path(sys.executable).parent)]),
                TEMP=self.temp.name, TMP=self.temp.name, USERPROFILE=self.temp.name, HOME=self.temp.name)
            for key in ('SystemDrive', 'ALLUSERSPROFILE', 'APPDATA', 'LOCALAPPDATA', 'ProgramData',
                        'ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432', 'USERNAME', 'USERDOMAIN'):
                if key in os.environ:
                    env[key] = os.environ[key]
            environment = c.create_unicode_buffer('\0'.join(f'{key}={value}' for key, value in sorted(env.items())) + '\0\0')
            create_process = api(self.kernel, 'CreateProcessW', [w.LPCWSTR, w.LPWSTR, c.c_void_p,
                c.c_void_p, w.BOOL, w.DWORD, c.c_void_p, w.LPCWSTR, c.POINTER(StartupEx), c.POINTER(ProcessInfo)])
            self._check(create_process(executable, c.create_unicode_buffer(f'"{executable}" /d /s /c "{command}"'),
                None, None, True, 0x80000 | 0x400 | 0x4 | 0x08000000, environment,
                str(workspace), c.byref(startup), c.byref(info)))
            self.process, self.thread, self.pid = info.process, info.thread, info.pid
            assign = api(self.kernel, 'AssignProcessToJobObject', [w.HANDLE, w.HANDLE])
            self._check(assign(self.job, self.process))
            self.resume_thread = api(self.kernel, 'ResumeThread', [w.HANDLE], w.DWORD)
        except BaseException as exc:
            try:
                self.close()
            except OSError as cleanup_error:
                exc.add_note(str(cleanup_error))
            raise

    def _check(self, value):
        if not value:
            raise SandboxUnavailable(str(self.c.WinError(self.c.get_last_error())))

    def start(self):
        if self.resume_thread(self.thread) == 0xffffffff:
            self._check(False)

    def _acl(self, path, operation, access):
        self.check()
        self._set_acl(path, {'grant': 1, 'deny': 3}[operation],
                      {'F': 0x1f01ff, 'M': 0x1301bf, 'RX': 0x1200a9}[access])
        self.granted.append((path, operation))

    def _allow_tree(self, path, access):
        if path == self.protected or path.is_relative_to(self.protected):
            return
        if path.is_symlink() or path.is_junction():
            return
        if access == 'M' and any(path == root or path.is_relative_to(root) for root in self.readonly):
            access = 'RX'
        boundaries = {self.protected, *self.readonly} if access == 'M' else {self.protected}
        if any(root != path and root.is_relative_to(path) for root in boundaries):
            self._set_acl(path, 1, {'M': 0x1301bf, 'RX': 0x1200a9}[access], inheritance=0)
            self.granted.append((path, 'grant'))
            for child in path.iterdir():
                self._allow_tree(child, access)
        else:
            self._acl(path, 'grant', access)

    def _set_acl(self, path, mode, mask, inheritance=3):
        c = self.c
        from ctypes import wintypes as w
        class Trustee(c.Structure):
            _fields_ = [('multiple', c.c_void_p), ('operation', c.c_int),
                        ('form', c.c_int), ('type', c.c_int), ('name', c.c_void_p)]
        class Access(c.Structure):
            _fields_ = [('mask', w.DWORD), ('mode', c.c_int), ('inheritance', w.DWORD), ('trustee', Trustee)]
        get = self.advapi.GetSecurityInfo
        get.argtypes = [w.HANDLE, c.c_int, w.DWORD, c.c_void_p, c.c_void_p,
                        c.POINTER(c.c_void_p), c.c_void_p, c.POINTER(c.c_void_p)]
        get.restype = w.DWORD
        entries = self.advapi.SetEntriesInAclW
        entries.argtypes = [w.ULONG, c.POINTER(Access), c.c_void_p, c.POINTER(c.c_void_p)]
        entries.restype = w.DWORD
        set_info = self.advapi.SetSecurityInfo
        set_info.argtypes = [w.HANDLE, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p]
        set_info.restype = w.DWORD
        old_acl, descriptor, new_acl = c.c_void_p(), c.c_void_p(), c.c_void_p()
        if path not in self.security_handles:
            create_file = self.kernel.CreateFileW
            create_file.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
            create_file.restype = w.HANDLE
            handle = create_file(str(path), 0x60000, 7, None, 3, 0x02000000, None)
            if handle == c.c_void_p(-1).value:
                self._check(False)
            self.security_handles[path] = handle
        handle = self.security_handles[path]
        try:
            error = get(handle, 1, 4, None, None, c.byref(old_acl), None, c.byref(descriptor))
            if error:
                raise SandboxUnavailable(str(c.WinError(error)))
            if mode == 0:
                if not old_acl:
                    raise SandboxUnavailable('Protected state has an unrestricted DACL')
                convert = self.advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
                convert.argtypes = [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), c.c_void_p]
                text = w.LPWSTR()
                self._check(convert(descriptor, 1, 4, c.byref(text), None))
                try:
                    if re.search(r'\((?:A|OA|XA|ZA);[^)]*;;;(?:AC|S-1-15-2-1|S-1-15-2-2)\)', text.value):
                        raise SandboxUnavailable('Protected state grants access to all application packages')
                finally:
                    self.local_free(c.cast(text, c.c_void_p))
                return
            entry = Access(mask, mode, inheritance, Trustee(None, 0, 0, 0, self.sid))
            error = entries(1, c.byref(entry), old_acl, c.byref(new_acl))
            if not error:
                error = set_info(handle, 1, 4, None, None, new_acl, None)
            if error:
                raise SandboxUnavailable(str(c.WinError(error)))
        finally:
            if descriptor: self.local_free(descriptor)
            if new_acl: self.local_free(new_acl)

    def poll(self):
        if self.returncode is None and self.wait_for(self.process, 0) == 0:
            from ctypes import wintypes as w
            code = w.DWORD()
            self._check(self.exit_code(self.process, self.c.byref(code)))
            self.returncode = code.value
        return self.returncode

    def close(self):
        try:
            self._close_resources()
        finally:
            if self.mutex_owned:
                self.release_mutex(self.mutex)
                self.mutex_owned = False
            if self.mutex:
                self.close_handle(self.mutex)
                self.mutex = None

    def _close_resources(self):
        # Closing the job also kills detached grandchildren after the shell exits.
        if self.job:
            if not self.kill_job(self.job, 1):
                self._check(False)
            # Wait for every member, not just the root shell, before removing ACLs.
            from ctypes import wintypes as w
            class Accounting(self.c.Structure):
                _fields_ = [('times', self.c.c_int64 * 4), ('faults', w.DWORD),
                            ('total', w.DWORD), ('active', w.DWORD), ('terminated', w.DWORD)]
            query = self.kernel.QueryInformationJobObject
            query.argtypes = [w.HANDLE, self.c.c_int, self.c.c_void_p, w.DWORD, self.c.c_void_p]
            info = Accounting()
            until = time.monotonic() + 5
            while True:
                if not query(self.job, 1, self.c.byref(info), self.c.sizeof(info), None):
                    self._check(False)
                if not info.active:
                    break
                if time.monotonic() >= until:
                    raise SandboxUnavailable('Sandbox descendants did not terminate; isolation retained')
                time.sleep(0.01)
            self.close_handle(self.job)
            self.job = None
        if self.process:
            # Also covers a suspended process whose job assignment failed.
            terminate = self.kernel.TerminateProcess
            terminate.argtypes = [self.c.c_void_p, self.c.c_uint]
            terminate(self.process, 1)
            self.wait_for(self.process, 5000)
        for handle in (self.thread, self.process):
            if handle:
                self.close_handle(handle)
        self.thread = self.process = None
        for fd in self._fds:
            os.close(fd)
        self._fds.clear()
        if self.attrs is not None:
            self.delete_attrs(self.attrs)
            self.attrs = None
        errors = []
        for path, operation in reversed(self.granted):
            try:
                self._set_acl(path, 4, 0)
            except OSError as exc:
                # A command may legitimately delete a granted workspace file.
                # Its ACL is gone with the file, so cleanup is already complete.
                if getattr(exc, 'winerror', None) != 2:
                    errors.append(str(exc))
        self.granted.clear()
        for handle in self.security_handles.values():
            self.close_handle(handle)
        self.security_handles.clear()
        if self.profile_created:
            self.delete_profile(self.name)
            self.profile_created = False
        if self.sid:
            self.free_sid(self.sid)
            self.sid = None
        try:
            self.temp.cleanup()
        except OSError as exc:
            errors.append(str(exc))
        if errors:
            raise SandboxUnavailable('Shell isolation cleanup failed: ' + '; '.join(errors))
