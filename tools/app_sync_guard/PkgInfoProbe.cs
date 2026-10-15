using System;
using System.Diagnostics;
using System.Runtime.InteropServices;

class PkgInfoProbe {
    const string Lib = "/usr/lib/libpkgmgr-info.so.0";
    const string AppId = "org.tizen.was-appsync";
    const uint Owner = 5001;

    [DllImport(Lib, CallingConvention = CallingConvention.Cdecl)]
    static extern int pkgmgrinfo_appinfo_get_usr_appinfo(string id, uint uid, out IntPtr info);
    [DllImport(Lib, CallingConvention = CallingConvention.Cdecl)]
    static extern int pkgmgrinfo_appinfo_get_usr_disabled_appinfo(string id, uint uid, out IntPtr info);
    [DllImport(Lib, CallingConvention = CallingConvention.Cdecl)]
    static extern int pkgmgrinfo_appinfo_get_exec(IntPtr info, out IntPtr value);
    [DllImport(Lib, CallingConvention = CallingConvention.Cdecl)]
    static extern int pkgmgrinfo_appinfo_is_disabled(IntPtr info, out byte value);
    [DllImport(Lib, CallingConvention = CallingConvention.Cdecl)]
    static extern int pkgmgrinfo_appinfo_is_support_disable(IntPtr info, out byte value);
    [DllImport(Lib, CallingConvention = CallingConvention.Cdecl)]
    static extern int pkgmgrinfo_appinfo_destroy_appinfo(IntPtr info);
    [DllImport("/usr/lib/libaul.so.0", CallingConvention = CallingConvention.Cdecl)]
    static extern int aul_reload_appinfo();
    [DllImport("libc.so.6")] static extern int setgroups(UIntPtr count, IntPtr groups);
    [DllImport("libc.so.6")] static extern int setgid(uint gid);
    [DllImport("libc.so.6")] static extern int setuid(uint uid);
    [DllImport("libc.so.6")] static extern uint getuid();

    static int Read(uint uid) {
        IntPtr info;
        int rc = pkgmgrinfo_appinfo_get_usr_appinfo(AppId, uid, out info);
        Console.WriteLine("uid={0} appinfo rc={1}", uid, rc);
        if (rc < 0) {
            rc = pkgmgrinfo_appinfo_get_usr_disabled_appinfo(AppId, uid, out info);
            Console.WriteLine("disabled appinfo rc={0}", rc);
            if (rc < 0) return 1;
        }
        try {
            IntPtr value;
            rc = pkgmgrinfo_appinfo_get_exec(info, out value);
            Console.WriteLine("exec rc={0} value={1}", rc, Marshal.PtrToStringAnsi(value));
            byte disabled;
            rc = pkgmgrinfo_appinfo_is_disabled(info, out disabled);
            Console.WriteLine("disabled rc={0} value={1}", rc, disabled);
            byte support;
            rc = pkgmgrinfo_appinfo_is_support_disable(info, out support);
            Console.WriteLine("support rc={0} value={1}", rc, support);
        } finally {
            pkgmgrinfo_appinfo_destroy_appinfo(info);
        }
        return 0;
    }

    // Maintenance only. Preserve the UID/GID used in the verified guard test.
    static int OwnerAction(string mode) {
        if (setgroups(UIntPtr.Zero, IntPtr.Zero) != 0 || setgid(Owner) != 0 ||
            setuid(Owner) != 0 || getuid() != Owner) return 3;
        Console.WriteLine("caller uid={0}", getuid());
        if (mode == "owner-reload") {
            int result = aul_reload_appinfo();
            Console.WriteLine("aul_reload_appinfo rc={0}", result);
            return result < 0 ? 1 : 0;
        }
        // Not read-only: if the guard fails, this can start app restoration.
        var start = new ProcessStartInfo("/usr/bin/app_launcher", "-s " + AppId);
        start.UseShellExecute = false;
        using (var process = Process.Start(start)) {
            if (!process.WaitForExit(10000)) {
                process.Kill();
                return 124;
            }
            return process.ExitCode;
        }
    }

    static int Usage() {
        Console.Error.WriteLine("Usage: PkgInfoProbe [UID|owner-reload|owner-launch]");
        return 2;
    }

    static int Main(string[] args) {
        if (args.Length > 1) return Usage();
        string mode = args.Length == 0 ? Owner.ToString() : args[0];
        if (mode == "owner-reload" || mode == "owner-launch") return OwnerAction(mode);
        uint uid;
        if (!UInt32.TryParse(mode, out uid)) return Usage();
        return Read(uid);
    }
}
