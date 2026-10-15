// One root job per launch. No remotely supplied commands: command is staged over SDB.
// The loopback socket only returns that job's cached result, authenticated by a
// per-job 256-bit secret. ACK permits cleanup; expiry cleans abandoned jobs.
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Diagnostics;
using System.Text;
using System.Threading;
using System.Runtime.InteropServices;

class SdbJob {
    [DllImport("libc")] static extern uint geteuid();
    [DllImport("libc", SetLastError=true)] static extern int kill(int pid, int sig);
    const int Cap = 1048576;
    sealed class Capture {
        public MemoryStream Data = new MemoryStream();
        public bool Truncated;
        public void Drain(Stream input) {
            try {
                byte[] b = new byte[8192]; int n;
                while ((n = input.Read(b, 0, b.Length)) > 0) {
                    int take = Math.Min(n, Cap - (int)Data.Length);
                    Data.Write(b, 0, take);
                    if (take < n) Truncated = true;
                }
            } catch (IOException) { }
        }
    }
    static bool ReadExact(Stream stream, byte[] b) {
        int pos=0, n;
        while (pos < b.Length) {
            n=stream.Read(b,pos,b.Length-pos);
            if (n==0) return false;
            pos+=n;
        }
        return true;
    }
    static bool Equal(byte[] a, byte[] b) {
        if (a.Length!=b.Length) return false;
        int diff=0; for(int i=0;i<a.Length;i++) diff |= a[i]^b[i];
        return diff==0;
    }
    static void Cleanup(string dir) {
        string prefix="/home/owner/share/tmp/sdk_tools/archive-root-";
        if (!dir.StartsWith(prefix, StringComparison.Ordinal) || dir.Length!=prefix.Length+16) return;
        foreach(char ch in dir.Substring(prefix.Length))
            if("0123456789abcdef".IndexOf(ch)<0) return;
        string name=Path.GetFileName(dir);
        if (name.IndexOfAny(new char[]{'/', '\\'}) >= 0) return;
        try { Directory.Delete(dir,true); } catch { }
        foreach(string ext in new string[]{".tar.gz", ".tar.gz.signature"}) {
            try { File.Delete("/home/owner/share/tmp/sdk_tools/on-demand/"+name+ext); } catch { }
        }
    }
    static byte[] Run(string dir, int timeout) {
        uint uid=geteuid();
        if(uid!=0) throw new InvalidOperationException("Root bootstrap failed");
        string command=File.ReadAllText(Path.Combine(dir,"command.txt"),Encoding.UTF8);
        if(command.Length>65536) throw new InvalidOperationException("Command too large");
        var psi=new ProcessStartInfo("/usr/bin/setsid","/bin/sh");
        psi.UseShellExecute=false;
        psi.RedirectStandardInput=true; psi.RedirectStandardOutput=true; psi.RedirectStandardError=true;
        var stdout=new Capture(); var stderr=new Capture();
        int code=125, flags=0;
        using(var p=new Process()) {
            p.StartInfo=psi; p.Start();
            var outThread=new Thread(()=>stdout.Drain(p.StandardOutput.BaseStream));
            var errThread=new Thread(()=>stderr.Drain(p.StandardError.BaseStream));
            outThread.IsBackground=true; errThread.IsBackground=true;
            outThread.Start(); errThread.Start();
            // Writer thread avoids blocking the timeout on a shell that stops reading stdin.
            var inputThread=new Thread(()=>{
                try { p.StandardInput.Write(command+"\n"); p.StandardInput.Close(); }
                catch(IOException) { } catch(ObjectDisposedException) { }
            });
            inputThread.IsBackground=true; inputThread.Start();
            if(!p.WaitForExit(timeout*1000)) {
                flags |= 1;
                kill(-p.Id,9); try { p.Kill(); } catch { }
                p.WaitForExit(3000); code=124;
            } else { code=p.ExitCode; }
            // Background descendants must close inherited output descriptors themselves.
            bool outDone=outThread.Join(3000), errDone=errThread.Join(3000);
            if(!outDone || !errDone) {
                kill(-p.Id,9); flags |= 8;
                if(!outThread.Join(2000) || !errThread.Join(2000))
                    throw new IOException("Output pipe did not close after process-group termination");
            }
        }
        if(stdout.Truncated) flags|=2;
        if(stderr.Truncated) flags|=4;
        using(var result=new MemoryStream()) using(var writer=new BinaryWriter(result)) {
            writer.Write(Encoding.ASCII.GetBytes("TSR1")); writer.Write(code);
            writer.Write((int)stdout.Data.Length); writer.Write((int)stderr.Data.Length);
            writer.Write(flags); writer.Write(uid);
            writer.Write(stdout.Data.ToArray()); writer.Write(stderr.Data.ToArray());
            return result.ToArray();
        }
    }
    static int Main(string[] args) {
        if(args.Length!=3) return 2;
        string dir=args[0]; int port=Int32.Parse(args[1]), timeout=Int32.Parse(args[2]);
        if(timeout<1 || timeout>300 || port<1024 || port>65535) return 2;
        byte[] secret=File.ReadAllBytes(Path.Combine(dir,"reply.key"));
        if(secret.Length!=32) return 2;
        var listener=new TcpListener(IPAddress.Loopback,port);
        try {
            listener.Start(2);
            byte[] result=Run(dir,timeout);
            DateTime expiry=DateTime.UtcNow.AddSeconds(90);
            while(DateTime.UtcNow<expiry) {
                if(!listener.Pending()) { Thread.Sleep(100); continue; }
                using(TcpClient client=listener.AcceptTcpClient()) {
                    client.ReceiveTimeout=3000; client.SendTimeout=10000;
                    try {
                        using(NetworkStream stream=client.GetStream()) {
                            byte[] received=new byte[32];
                            if(!ReadExact(stream,received) || !Equal(received,secret)) continue;
                            stream.Write(result,0,result.Length); stream.Flush();
                            byte[] ack=new byte[4];
                            if(ReadExact(stream,ack) && Encoding.ASCII.GetString(ack)=="ACK1") return 0;
                        }
                    } catch(IOException) { } catch(SocketException) { }
                }
            }
            return 4;
        } catch(Exception e) {
            Console.Error.WriteLine(e.GetType().Name+": "+e.Message); return 3;
        } finally { listener.Stop(); Cleanup(dir); }
    }
}
