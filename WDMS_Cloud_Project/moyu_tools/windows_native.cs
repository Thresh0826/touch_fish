// Compiled locally by Windows PowerShell; no downloads, COM tree walk or network.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using Accessibility;

public static class MoyuNative {
    [StructLayout(LayoutKind.Sequential)] public struct Point { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] public struct Rect { public int L, T, R, B; }
    [StructLayout(LayoutKind.Sequential)] struct ScrollInfo {
        public uint Size, Mask; public int Min, Max; public uint Page; public int Pos, Track;
    }
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] static extern IntPtr WindowFromPoint(Point point);
    [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
    [DllImport("user32.dll")] static extern IntPtr GetParent(IntPtr hwnd);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr hwnd,uint relation);
    [DllImport("user32.dll",EntryPoint="GetWindowLongW")] static extern int GetWindowLong(IntPtr hwnd,int index);
    delegate bool EnumChild(IntPtr hwnd, IntPtr data);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumChild callback, IntPtr data);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int count);
    [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr hwnd, out Rect rect);
    [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr hwnd, ref Point point);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr hwnd, EnumChild callback, IntPtr data);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsWindowEnabled(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr hwnd,uint attribute,out uint value,int size);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, StringBuilder text, int count);
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr SendMessageTimeout(IntPtr hwnd, uint msg, UIntPtr wp, IntPtr lp, uint flags, uint timeout, out UIntPtr result);
    [DllImport("user32.dll", EntryPoint="SendMessageTimeoutW", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr SendTextTimeout(IntPtr hwnd,uint msg,UIntPtr wp,StringBuilder text,uint flags,uint timeout,out UIntPtr result);
    [DllImport("user32.dll")] static extern bool GetScrollInfo(IntPtr hwnd, int bar, ref ScrollInfo info);
    [DllImport("user32.dll")] static extern int SetScrollInfo(IntPtr hwnd, int bar, ref ScrollInfo info, bool redraw);
    [DllImport("oleacc.dll")] static extern int AccessibleObjectFromPoint(Point pt, [MarshalAs(UnmanagedType.Interface)] out IAccessible obj, [MarshalAs(UnmanagedType.Struct)] out object child);
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr VirtualAllocEx(IntPtr process, IntPtr address, UIntPtr bytes, uint allocation, uint protect);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool VirtualFreeEx(IntPtr process, IntPtr address, UIntPtr bytes, uint type);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool WriteProcessMemory(IntPtr process, IntPtr address, byte[] bytes, UIntPtr size, out UIntPtr written);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool ReadProcessMemory(IntPtr process, IntPtr address, byte[] bytes, UIntPtr size, out UIntPtr read);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("user32.dll")] static extern bool OpenClipboard(IntPtr owner);
    [DllImport("user32.dll")] static extern bool CloseClipboard();
    [DllImport("user32.dll")] static extern bool EmptyClipboard();
    [DllImport("user32.dll")] static extern uint GetClipboardSequenceNumber();
    [DllImport("user32.dll")] static extern IntPtr GetClipboardData(uint format);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern uint RegisterClipboardFormat(string name);
    [DllImport("kernel32.dll")] static extern IntPtr GlobalLock(IntPtr handle);
    [DllImport("kernel32.dll")] static extern bool GlobalUnlock(IntPtr handle);
    [DllImport("kernel32.dll")] static extern UIntPtr GlobalSize(IntPtr handle);
    [DllImport("shell32.dll", CharSet=CharSet.Unicode)] static extern uint DragQueryFile(IntPtr drop, uint index, StringBuilder text, uint size);
    static Timer guardian;

    public static void Initialize(int parent) {
        SetProcessDPIAware();
        var process = Process.GetProcessById(parent);
        long started = process.StartTime.ToUniversalTime().Ticks;
        guardian = new Timer(delegate(object unused) {
            try {
                var p = Process.GetProcessById(parent);
                if(p.HasExited || p.StartTime.ToUniversalTime().Ticks != started) Environment.Exit(3);
            } catch { Environment.Exit(3); }
        }, null, 500, 500);
        GC.KeepAlive(guardian);
    }

    public static Dictionary<string,object> Metadata(long handle) {
        IntPtr hwnd = new IntPtr(handle); uint pid; Rect rect;
        if(GetWindowThreadProcessId(hwnd, out pid)==0 || !GetWindowRect(hwnd, out rect)) throw new InvalidOperationException("window_missing");
        var name = new StringBuilder(256); GetClassName(hwnd,name,name.Capacity);
        return new Dictionary<string,object> { {"hwnd", handle}, {"pid", pid}, {"class_name",name.ToString()},
            {"rect", new int[]{rect.L,rect.T,rect.R,rect.B}}, {"dpi",GetDpiForWindow(hwnd)},
            {"visible",IsWindowVisible(hwnd)}, {"minimized",IsIconic(hwnd)} };
    }

    public static void Guard(long handle, uint pid, int[] expected, bool foreground) {
        uint current; Rect rect; IntPtr hwnd = new IntPtr(handle);
        if(GetWindowThreadProcessId(hwnd,out current)==0)
            throw new InvalidOperationException("window_missing");
        if(current!=pid)
            throw new InvalidOperationException(string.Format("window_pid_changed: expected {0}, got {1}", pid, current));
        if(!GetWindowRect(hwnd,out rect))
            throw new InvalidOperationException("window_rect_failed");
        if(!IsWindowVisible(hwnd))
            throw new InvalidOperationException("window_not_visible");
        if(IsIconic(hwnd))
            throw new InvalidOperationException("window_minimized");
        if(rect.L!=expected[0] || rect.T!=expected[1] || rect.R!=expected[2] || rect.B!=expected[3])
            throw new InvalidOperationException(string.Format("window_rect_changed: expected [{0},{1},{2},{3}], got [{4},{5},{6},{7}]",
                expected[0], expected[1], expected[2], expected[3], rect.L, rect.T, rect.R, rect.B));
        if(foreground) {
            IntPtr fg = GetForegroundWindow();
            if(fg != hwnd) {
                uint fgPid;
                GetWindowThreadProcessId(fg, out fgPid);
                long root2 = GetAncestor(fg, 2).ToInt64();
                long root3 = GetAncestor(fg, 3).ToInt64();
                bool sameProcess = (fgPid != 0 && fgPid == pid);
                bool isDescendant = (fg == hwnd || root2 == handle || root3 == handle);
                if(!sameProcess || !isDescendant)
                    throw new InvalidOperationException(string.Format("window_not_foreground: expected 0x{0:X} (pid {1}), got 0x{2:X} (pid {3})",
                        handle, pid, fg.ToInt64(), fgPid));
            }
        }
    }

    public static Dictionary<string,object> PointInfo(int x, int y) {
        Point pt = new Point {X=x,Y=y}; IntPtr hwnd = WindowFromPoint(pt);
        IntPtr root = GetAncestor(hwnd,2);
        var data = Metadata(hwnd.ToInt64()); data["root"] = Metadata(root.ToInt64());
        data["point"] = new int[]{x,y};
        return data;
    }

    public static object[] Windows() {
        var result=new List<object>(); int visited=0;
        EnumChild callback=delegate(IntPtr hwnd,IntPtr unused) {
            if(++visited>512) return false;
            if(!IsWindowVisible(hwnd) || IsIconic(hwnd) || GetWindow(hwnd,4)!=IntPtr.Zero) return true;
            var text=new StringBuilder(512); GetWindowText(hwnd,text,512);
            if(text.Length==0) return true;
            try { var data=Metadata(hwnd.ToInt64()); data["title"]=text.ToString(); result.Add(data); }
            catch(InvalidOperationException) { /* A closing window is not a selectable target. */ }
            return true;
        };
        EnumWindows(callback,IntPtr.Zero); GC.KeepAlive(callback);
        return result.ToArray();
    }

    public static Dictionary<string,object> Preview(long root,uint pid,int maxWidth,int maxHeight) {
        if(maxWidth<100 || maxHeight<100 || maxWidth>2000 || maxHeight>1500)
            throw new InvalidOperationException("preview_size_invalid");
        IntPtr hwnd=new IntPtr(root); Rect client;
        if(!GetClientRect(hwnd,out client)) throw new InvalidOperationException("window_client_rect_failed");
        Point origin=new Point{X=0,Y=0};
        if(!ClientToScreen(hwnd,ref origin)) throw new InvalidOperationException("window_client_to_screen_failed");
        int[] area=new int[]{origin.X,origin.Y,origin.X+client.R,origin.Y+client.B};
        Unobscured(area,root,pid);
        double scale=Math.Min(1.0,Math.Min((double)maxWidth/client.R,(double)maxHeight/client.B));
        int width=Math.Max(1,(int)(client.R*scale)), height=Math.Max(1,(int)(client.B*scale));
        string png;
        using(var source=new Bitmap(client.R,client.B,PixelFormat.Format24bppRgb)) {
            using(var graphics=Graphics.FromImage(source)) graphics.CopyFromScreen(origin.X,origin.Y,0,0,source.Size);
            using(var resized=new Bitmap(width,height,PixelFormat.Format24bppRgb)) {
                using(var graphics=Graphics.FromImage(resized)) {
                    graphics.InterpolationMode=System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
                    graphics.DrawImage(source,0,0,width,height);
                }
                using(var stream=new MemoryStream()) { resized.Save(stream,ImageFormat.Png); png=Convert.ToBase64String(stream.ToArray()); }
            }
        }
        var controls=new List<object>(); int visited=0; var clock=Stopwatch.StartNew();
        EnumChild callback=delegate(IntPtr child,IntPtr unused) {
            if(++visited>512 || clock.ElapsedMilliseconds>3000) return false;
            if(!IsWindowVisible(child)) return true;
            Dictionary<string,object> data;
            try { data=Metadata(child.ToInt64()); }
            catch(InvalidOperationException) { return true; }
            string cls=(string)data["class_name"], caption="";
            if(Regex.IsMatch(cls,@"(^|\.)(BUTTON|STATIC)(\.|$)",RegexOptions.IgnoreCase)) {
                var text=new StringBuilder(256); UIntPtr count;
                if(SendTextTimeout(child,0x000D,new UIntPtr(256),text,0x23,100,out count)!=IntPtr.Zero && count.ToUInt64()<255)
                    caption=text.ToString();
            }
            int[] box=(int[])data["rect"];
            if(box[0]<box[2] && box[1]<box[3]) {
                data["text"]=caption; data["point"]=new int[]{(box[0]+box[2])/2,(box[1]+box[3])/2}; controls.Add(data);
            }
            return true;
        };
        EnumChildWindows(hwnd,callback,IntPtr.Zero); GC.KeepAlive(callback);
        int countY=int.MaxValue;
        foreach(Dictionary<string,object> item in controls) {
            string caption=(string)item["text"];
            if(Regex.IsMatch(caption,@"^\s*总行数")) countY=Math.Min(countY,((int[])item["rect"])[3]);
        }
        Dictionary<string,object> grid=null;
        foreach(Dictionary<string,object> item in controls) {
            int[] box=(int[])item["rect"];
            if(box[1]<countY || box[2]-box[0]<250 || box[3]-box[1]<60) continue;
            if(grid==null || box[1]<((int[])grid["rect"])[1]) grid=item;
        }
        if(grid!=null) {
            int[] box=(int[])grid["rect"]; int probes=0;
            for(int x=box[0]+10;x<box[2] && probes<64 && clock.ElapsedMilliseconds<5000;x+=16,probes++) {
                try {
                    var read=ReadPoint(x,box[1]+10,root,pid);
                    if((string)read["name"]!="送货单号" || read["rect"]==null) continue;
                    int[] cell=(int[])read["rect"];
                    int cx=(cell[0]+cell[2])/2, cy=(cell[1]+cell[3])/2;
                    var header=PointInfo(cx,cy); header.Remove("root"); header["hint"]="header";
                    header["accessible_rect"]=cell; controls.Add(header);
                    var first=PointInfo(cx,cell[3]+Math.Max(8,(cell[3]-cell[1])/2)); first.Remove("root"); first["hint"]="cell";
                    controls.Add(first); break;
                } catch(COMException) { /* Optional header candidates never authorize a guessed value. */ }
                  catch(InvalidOperationException) { /* Manual snapshot marking remains available. */ }
            }
        }
        Unobscured(area,root,pid);
        return new Dictionary<string,object>{{"root",Metadata(root)},{"source_rect",area},{"width",width},{"height",height},
            {"image",png},{"controls",controls.ToArray()}};
    }

    public static Dictionary<string,object> ReadPoint(int x, int y,long root,uint pid) {
        Point point=new Point{X=x,Y=y};
        GuardChild(WindowFromPoint(point),root,pid);
        IAccessible accessible; object child;
        if(AccessibleObjectFromPoint(point,out accessible,out child)!=0 || accessible==null)
            throw new InvalidOperationException("accessible_unavailable");
        string name="", value=""; object role=null;
        try { name=accessible.get_accName(child) ?? ""; } catch(COMException) {}
        try { value=accessible.get_accValue(child) ?? ""; } catch(COMException) {}
        try { role=accessible.get_accRole(child); } catch(COMException) {}
        int[] bounds=null;
        try {
            int left,top,width,height;
            accessible.accLocation(out left,out top,out width,out height,child);
            if(width>0 && height>0) bounds=new int[]{left,top,left+width,top+height};
        } catch(COMException) {}
        GuardChild(WindowFromPoint(point),root,pid);
        return new Dictionary<string,object>{{"name",name},{"value",value},{"role",role},{"rect",bounds}};
    }

    static void GuardChild(IntPtr hwnd,long root,uint expectedPid) {
        if(hwnd == IntPtr.Zero)
            throw new InvalidOperationException("child_window_missing");
        uint pid;
        if(GetWindowThreadProcessId(hwnd,out pid)==0)
            throw new InvalidOperationException("child_window_missing");
        if(pid!=expectedPid)
            throw new InvalidOperationException(string.Format("child_pid_mismatch: expected {0}, got {1} (hwnd=0x{2:X})", expectedPid, pid, hwnd.ToInt64()));
        long root2 = GetAncestor(hwnd, 2).ToInt64();
        long root3 = GetAncestor(hwnd, 3).ToInt64();
        if(hwnd.ToInt64() != root && root2 != root && root3 != root)
            throw new InvalidOperationException(string.Format("child_root_mismatch: expected 0x{0:X}, got root2=0x{1:X}, root3=0x{2:X} (hwnd=0x{3:X})", root, root2, root3, hwnd.ToInt64()));
        if(!IsWindowVisible(hwnd))
            throw new InvalidOperationException(string.Format("child_not_visible: hwnd=0x{0:X}", hwnd.ToInt64()));
    }

    static void Message(IntPtr hwnd,uint msg,UIntPtr wp,IntPtr lp,out UIntPtr result) {
        if(SendMessageTimeout(hwnd,msg,wp,lp,0x23,500,out result)==IntPtr.Zero)
            throw new InvalidOperationException("native_message_timeout");
    }

    // WM_GETTEXT/WM_SETTEXT are system messages: Windows marshals text between
    // processes. Do not use the clipboard or guess a keyboard focus target.
    public static string EditValue(long handle,string value,bool write,long root,uint pid) {
        IntPtr hwnd=new IntPtr(handle);
        GuardChild(hwnd,root,pid);
        var cls=new StringBuilder(256); GetClassName(hwnd,cls,256);
        if(!Regex.IsMatch(cls.ToString(),@"(^|\.)EDIT(\.|$)",RegexOptions.IgnoreCase))
            throw new InvalidOperationException("edit_control_unsupported");
        UIntPtr result;
        if(write) {
            if(!IsWindowEnabled(hwnd) || (GetWindowLong(hwnd,-16)&0x800)!=0 || value==null ||
               !Regex.IsMatch(value,@"\A[A-Za-z0-9][A-Za-z0-9_-]{0,79}\z"))
                throw new InvalidOperationException("edit_value_rejected");
            GuardChild(hwnd,root,pid);
            var input=new StringBuilder(value);
            if(SendTextTimeout(hwnd,0x000C,UIntPtr.Zero,input,0x23,500,out result)==IntPtr.Zero || result==UIntPtr.Zero)
                throw new InvalidOperationException("edit_write_unconfirmed");
        }
        var text=new StringBuilder(256);
        if(SendTextTimeout(hwnd,0x000D,new UIntPtr(256),text,0x23,500,out result)==IntPtr.Zero || result.ToUInt64()>=255)
            throw new InvalidOperationException("edit_read_unconfirmed");
        GuardChild(hwnd,root,pid);
        string actual=text.ToString();
        if(write && !String.Equals(actual,value,StringComparison.Ordinal))
            throw new InvalidOperationException("edit_write_unconfirmed");
        return actual;
    }

    // DateTimePicker messages contain pointers above WM_USER; explicitly marshal
    // SYSTEMTIME in the target process, rather than passing a local pointer.
    public static string DateValue(long handle, string value, bool write,long root,uint expectedPid) {
        IntPtr hwnd=new IntPtr(handle); uint pid; GetWindowThreadProcessId(hwnd,out pid);
        GuardChild(hwnd,root,expectedPid);
        var cls=new StringBuilder(256); GetClassName(hwnd,cls,256);
        if(cls.ToString().IndexOf("DateTimePick",StringComparison.OrdinalIgnoreCase)<0)
            throw new InvalidOperationException("date_control_unsupported");
        IntPtr process=OpenProcess(0x0038,false,pid);
        if(process==IntPtr.Zero) throw new InvalidOperationException("date_access_denied");
        IntPtr memory=IntPtr.Zero;
        try {
            memory=VirtualAllocEx(process,IntPtr.Zero,new UIntPtr(16),0x3000,0x04);
            if(memory==IntPtr.Zero) throw new InvalidOperationException("date_buffer_failed");
            UIntPtr count,result;
            if(write) {
                var date=DateTime.ParseExact(value,"yyyy-MM-dd",System.Globalization.CultureInfo.InvariantCulture);
                Message(hwnd,0x1001,UIntPtr.Zero,memory,out result);
                var bytes=new byte[16];
                if(result!=UIntPtr.Zero || !ReadProcessMemory(process,memory,bytes,new UIntPtr(16),out count) || count.ToUInt64()!=16)
                    throw new InvalidOperationException("date_read_failed");
                var parts=new ushort[8]; Buffer.BlockCopy(bytes,0,parts,0,16);
                parts[0]=(ushort)date.Year; parts[1]=(ushort)date.Month;
                parts[2]=(ushort)date.DayOfWeek; parts[3]=(ushort)date.Day;
                // Preserve the existing hidden time component of each control.
                Buffer.BlockCopy(parts,0,bytes,0,16);
                if(!WriteProcessMemory(process,memory,bytes,new UIntPtr(16),out count) || count.ToUInt64()!=16)
                    throw new InvalidOperationException("date_write_failed");
                GuardChild(hwnd,root,expectedPid);
                Message(hwnd,0x1002,UIntPtr.Zero,memory,out result);
                if(result==UIntPtr.Zero) throw new InvalidOperationException("date_not_set");
            }
            Message(hwnd,0x1001,UIntPtr.Zero,memory,out result);
            if(result!=UIntPtr.Zero) throw new InvalidOperationException("date_not_valid");
            var output=new byte[16];
            if(!ReadProcessMemory(process,memory,output,new UIntPtr(16),out count) || count.ToUInt64()!=16)
                throw new InvalidOperationException("date_read_failed");
            var fields=new ushort[8]; Buffer.BlockCopy(output,0,fields,0,16);
            return new DateTime(fields[0],fields[1],fields[3]).ToString("yyyy-MM-dd");
        } finally {
            if(memory!=IntPtr.Zero) VirtualFreeEx(process,memory,UIntPtr.Zero,0x8000);
            CloseHandle(process);
        }
    }

    public static int Scroll(long handle, int position, bool restore,long root,uint expectedPid) {
        var info=new ScrollInfo{Size=(uint)Marshal.SizeOf(typeof(ScrollInfo)),Mask=0x17};
        IntPtr hwnd=new IntPtr(handle);
        GuardChild(hwnd,root,expectedPid);
        IntPtr bar=hwnd; int barType=0;
        if(!GetScrollInfo(hwnd,0,ref info)) {
            // WinForms DataGridView often hosts a separate HScrollBar rather
            // than a native WS_HSCROLL on the grid itself. Inspect immediate
            // children only; never choose a scrollbar from the lower detail grid.
            var candidates=new List<IntPtr>();
            IntPtr child=GetWindow(hwnd,5); int nodes=0;
            while(child!=IntPtr.Zero) {
                if(++nodes>128) throw new InvalidOperationException("scroll_scan_limit");
                var cls=new StringBuilder(256); GetClassName(child,cls,256);
                if(IsWindowVisible(child) && cls.ToString().IndexOf("SCROLLBAR",StringComparison.OrdinalIgnoreCase)>=0 &&
                   (GetWindowLong(child,-16)&1)==0) candidates.Add(child);
                child=GetWindow(child,2);
            }
            if(candidates.Count!=1) return -1;
            bar=candidates[0]; barType=2;
            if(!GetScrollInfo(bar,barType,ref info)) return -1;
        }
        if(restore) {
            if(position<info.Min || position>info.Max || position>65535)
                throw new InvalidOperationException("scroll_position_changed");
            GuardChild(hwnd,root,expectedPid);
            GuardChild(bar,root,expectedPid);
            info.Mask=4; info.Pos=position; SetScrollInfo(bar,barType,ref info,true);
            UIntPtr result;
            Message(hwnd,0x0114,new UIntPtr((uint)(4 | (position<<16))),barType==2 ? bar : IntPtr.Zero,out result);
        }
        return info.Pos;
    }

    public static string Conditions(long root,int[] region,long[] dates) {
        // "dates" retains the v1 protocol name; v2 also excludes the contract
        // edit. Its exact value is verified separately against each source row.
        CheckRect(region);
        var values=new List<string>(); int visited=0; Exception failure=null;
        EnumChild callback=delegate(IntPtr hwnd,IntPtr unused) {
            try {
                if(++visited>512) throw new InvalidOperationException("conditions_scan_limit");
                if(!IsWindowVisible(hwnd)) return true;
                Rect rect;
                if(!GetWindowRect(hwnd,out rect) || rect.L<region[0] || rect.T<region[1] || rect.R>region[2] || rect.B>region[3]) return true;
                IntPtr ancestor=hwnd;
                for(int level=0;level<16 && ancestor!=IntPtr.Zero;level++) {
                    if(Array.IndexOf(dates,ancestor.ToInt64())>=0) return true;
                    ancestor=GetParent(ancestor);
                }
                var cls=new StringBuilder(256); GetClassName(hwnd,cls,256);
                string name=cls.ToString();
                if(!Regex.IsMatch(name,@"(^|\.)(EDIT|COMBOBOX|BUTTON|STATIC)(\.|$)",RegexOptions.IgnoreCase)) return true;
                UIntPtr result; var text=new StringBuilder(2048);
                if(SendTextTimeout(hwnd,0x000D,new UIntPtr(2048),text,0x23,500,out result)==IntPtr.Zero || result.ToUInt64()>=2047)
                    throw new InvalidOperationException("conditions_text_unavailable");
                string check="";
                if(name.IndexOf("BUTTON",StringComparison.OrdinalIgnoreCase)>=0) {
                    Message(hwnd,0x00F0,UIntPtr.Zero,IntPtr.Zero,out result);
                    check=result.ToUInt64().ToString();
                }
                // Native handles and WinForms .app suffixes change after restart.
                // Order fields by relative visual position, hash only their semantic
                // type/value/check state so explicit rebind can retain old receipts.
                name=Regex.Replace(name,@"\.app\..*$", "", RegexOptions.IgnoreCase);
                values.Add((rect.T-region[1]).ToString("D8",System.Globalization.CultureInfo.InvariantCulture)+"|"+
                           (rect.L-region[0]).ToString("D8",System.Globalization.CultureInfo.InvariantCulture)+"|"+
                           name.Length.ToString()+":"+name+"|"+text.Length.ToString()+":"+text.ToString()+"|"+check);
                return true;
            } catch(Exception error) { failure=error; return false; }
        };
        EnumChildWindows(new IntPtr(root),callback,IntPtr.Zero);
        GC.KeepAlive(callback);
        if(failure!=null) throw failure;
        if(values.Count==0) throw new InvalidOperationException("conditions_text_unavailable");
        values.Sort(StringComparer.Ordinal);
        for(int i=0;i<values.Count;i++) values[i]=values[i].Substring(18);
        using(var hash=SHA256.Create()) return BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(String.Join("\n",values)))).Replace("-","").ToLowerInvariant();
    }

    static void CheckRect(int[] rect) {
        if(rect.Length!=4 || rect[2]<=rect[0] || rect[3]<=rect[1] || rect[2]-rect[0]>4096 ||
           rect[3]-rect[1]>4096 || (long)(rect[2]-rect[0])*(rect[3]-rect[1])>8000000)
            throw new InvalidOperationException("capture_rect_invalid");
    }
    public static void Unobscured(int[] rect,long root,uint pid) {
        CheckRect(rect);
        int w = rect[2] - rect[0], h = rect[3] - rect[1];
        int padX = w >= 20 ? 4 : 0;
        int padY = h >= 20 ? 4 : 0;
        int minX = rect[0] + padX, maxX = rect[2] - 1 - padX;
        int minY = rect[1] + padY, maxY = rect[3] - 1 - padY;
        for(int y=0;y<3;y++) for(int x=0;x<3;x++) {
            Point point=new Point{X=minX+(maxX-minX)*x/2,Y=minY+(maxY-minY)*y/2};
            GuardChild(WindowFromPoint(point),root,pid);
        }
        // A small always-on-top overlay could fall between sample points.
        // Reject any visible overlapping top-level window above this root.
        IntPtr above=GetWindow(new IntPtr(root),3); int visited=0;
        while(above!=IntPtr.Zero) {
            if(++visited>256) throw new InvalidOperationException("capture_scan_limit");
            Rect box; uint cloaked;
            bool hidden=DwmGetWindowAttribute(above,14,out cloaked,4)==0 && cloaked!=0;
            if(!hidden && IsWindowVisible(above) && !IsIconic(above) && GetWindowRect(above,out box) &&
               box.L<rect[2] && box.R>rect[0] && box.T<rect[3] && box.B>rect[1])
                throw new InvalidOperationException("window_obscured");
            above=GetWindow(above,3);
        }
    }
    public static byte[] Capture(int[] rect) {
        CheckRect(rect);
        using(var bitmap=new Bitmap(rect[2]-rect[0],rect[3]-rect[1],PixelFormat.Format32bppArgb)) {
            using(var graphics=Graphics.FromImage(bitmap)) {
                graphics.CopyFromScreen(rect[0],rect[1],0,0,bitmap.Size,CopyPixelOperation.SourceCopy);
            }
            using(var stream=new MemoryStream()) { bitmap.Save(stream,ImageFormat.Png); return stream.ToArray(); }
        }
    }
    public static string Hash(int[] rect) {
        using(var hash=SHA256.Create()) return BitConverter.ToString(hash.ComputeHash(Capture(rect))).Replace("-","").ToLowerInvariant();
    }
    public static uint ClearClipboard() {
        if(!OpenClipboard(IntPtr.Zero)) throw new InvalidOperationException("clipboard_busy");
        try { if(!EmptyClipboard()) throw new InvalidOperationException("clipboard_clear_failed"); }
        finally { CloseClipboard(); }
        return GetClipboardSequenceNumber();
    }
    public static Dictionary<string,object> ClipboardText() {
        uint before=GetClipboardSequenceNumber();
        string text=System.Windows.Forms.Clipboard.ContainsText() ? System.Windows.Forms.Clipboard.GetText() : "";
        if(before!=GetClipboardSequenceNumber()) throw new InvalidOperationException("clipboard_changed");
        return new Dictionary<string,object>{{"sequence",before},{"text",text}};
    }
    public static Dictionary<string,object> ClipboardFile() {
        uint sequence=GetClipboardSequenceNumber();
        if(!OpenClipboard(IntPtr.Zero)) throw new InvalidOperationException("clipboard_busy");
        try {
            string name=null; long? size=null;
            IntPtr data=GetClipboardData(RegisterClipboardFormat("FileGroupDescriptorW"));
            if(data!=IntPtr.Zero) {
                ulong length=GlobalSize(data).ToUInt64();
                if(length<596 || length>65536) throw new InvalidOperationException("file_descriptor_invalid");
                IntPtr pointer=GlobalLock(data);
                if(pointer==IntPtr.Zero) throw new InvalidOperationException("clipboard_locked");
                try {
                    if(Marshal.ReadInt32(pointer)!=1) throw new InvalidOperationException("file_count_not_one");
                    uint flags=unchecked((uint)Marshal.ReadInt32(pointer,4));
                    uint attributes=unchecked((uint)Marshal.ReadInt32(pointer,40));
                    if((flags&4)!=0 && (attributes&16)!=0) throw new InvalidOperationException("clipboard_is_directory");
                    name=Marshal.PtrToStringUni(IntPtr.Add(pointer,76),260).Split('\0')[0];
                    if((flags&64)!=0) {
                        ulong high=unchecked((uint)Marshal.ReadInt32(pointer,68));
                        ulong low=unchecked((uint)Marshal.ReadInt32(pointer,72));
                        ulong total=(high<<32)|low;
                        if(total>long.MaxValue) throw new InvalidOperationException("file_size_invalid");
                        size=(long)total;
                    }
                } finally { GlobalUnlock(data); }
            } else {
                data=GetClipboardData(15); // CF_HDROP, e.g. redirected remote drives.
                if(data==IntPtr.Zero) throw new InvalidOperationException("file_clipboard_missing");
                if(DragQueryFile(data,0xffffffff,null,0)!=1) throw new InvalidOperationException("file_count_not_one");
                var text=new StringBuilder(32768);
                if(DragQueryFile(data,0,text,(uint)text.Capacity)==0) throw new InvalidOperationException("drop_name_missing");
                name=Path.GetFileName(text.ToString());
            }
            if(sequence!=GetClipboardSequenceNumber()) throw new InvalidOperationException("clipboard_changed");
            return new Dictionary<string,object>{{"sequence",sequence},{"name",name},{"size",size}};
        } finally { CloseClipboard(); }
    }
}
