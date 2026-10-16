param(
    [Parameter(Mandatory=$true)][long]$RootHwnd,
    [Parameter(Mandatory=$true)][long]$ProcessId,
    [Parameter(Mandatory=$true)][long]$ContainerHwnd,
    [Parameter(Mandatory=$true)][string]$ExpectedRect,
    [Parameter(Mandatory=$true)][string]$TemplateFile,
    [Parameter(Mandatory=$true)][string]$OutputFile
)
$ErrorActionPreference = 'Stop'
$phase = 'assembly_load'
function Write-Result($value) {
    [IO.File]::WriteAllText($OutputFile, (ConvertTo-Json -InputObject $value -Depth 5 -Compress),
                          (New-Object Text.UTF8Encoding($false)))
}
try {
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.Windows.Forms
    $accessibility = [Reflection.Assembly]::Load('Accessibility, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a')
    $drawing = [System.Drawing.Bitmap].Assembly
    $forms = [System.Windows.Forms.SystemInformation].Assembly
    $system = [Reflection.Assembly]::Load('System, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089')
    $phase = 'source_read'
    $matcher = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'toolbar_icon_matcher.cs'))
    $capture = @'
public static class WdmsToolbarIconCapture {
    public sealed class Output { public WdmsToolbarIconMatcher.Match Match; public int[] Button; }
    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    public struct Rect { public int L,T,R,B; }
    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    public struct Point { public int X,Y; }
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool IsWindowVisible(System.IntPtr h);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool IsWindowEnabled(System.IntPtr h);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool IsChild(System.IntPtr root,System.IntPtr h);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool IsIconic(System.IntPtr h);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(System.IntPtr h,out uint pid);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool GetWindowRect(System.IntPtr h,out Rect r);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern System.IntPtr GetForegroundWindow();
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool SetForegroundWindow(System.IntPtr h);
    [System.Runtime.InteropServices.DllImport("user32.dll")] static extern System.IntPtr WindowFromPoint(Point p);
    [System.Runtime.InteropServices.DllImport("oleacc.dll")]
    static extern int AccessibleObjectFromPoint(Point point,
        [System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Interface)] out Accessibility.IAccessible accessible,
        [System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Struct)] out object child);

    static System.Drawing.Rectangle Validate(long rootId,long processId,long toolbarId,string expected) {
        System.IntPtr root=new System.IntPtr(rootId),toolbar=new System.IntPtr(toolbarId);
        uint p1,p2;Rect r,main;
        GetWindowThreadProcessId(root,out p1);GetWindowThreadProcessId(toolbar,out p2);
        if(p1!=processId || p2!=processId || !IsWindowVisible(root) || IsIconic(root) ||
           !IsWindowEnabled(root) || !IsWindowVisible(toolbar) || !IsWindowEnabled(toolbar) ||
           !IsChild(root,toolbar) || !GetWindowRect(toolbar,out r) || !GetWindowRect(root,out main))
            throw new System.Exception("ICON_CONTAINER_CHANGED");
        if(r.L+","+r.T+","+r.R+","+r.B!=expected || r.B-r.T<24 || r.B-r.T>160 ||
           r.R-r.L<32 || r.R-r.L>4096 || r.L<main.L || r.R>main.R || r.T<main.T || r.B>main.T+180)
            throw new System.Exception("ICON_CONTAINER_CHANGED");
        System.Drawing.Rectangle box=System.Drawing.Rectangle.FromLTRB(r.L,r.T,r.R,r.B);
        if(!System.Windows.Forms.SystemInformation.VirtualScreen.Contains(box))
            throw new System.Exception("ICON_CONTAINER_CHANGED");
        return box;
    }
    static int[] Button(System.Drawing.Rectangle toolbar,long rootId,long toolbarId,WdmsToolbarIconMatcher.Match match) {
        Point p=new Point { X=toolbar.Left+match.X+match.Width/2,Y=toolbar.Top+match.Y+match.Height/2 };
        if(GetForegroundWindow()!=new System.IntPtr(rootId) || WindowFromPoint(p)!=new System.IntPtr(toolbarId))
            throw new System.Exception("ICON_TARGET_OBSCURED");
        Accessibility.IAccessible accessible=null;object child=null;
        try {
            if(AccessibleObjectFromPoint(p,out accessible,out child)!=0 || accessible==null || !(child is int))
                throw new System.Exception("ICON_BUTTON_UNCONFIRMED");
            object roleValue=accessible.get_accRole(child),stateValue=accessible.get_accState(child);
            if(!(roleValue is int) || !(stateValue is int))
                throw new System.Exception("ICON_BUTTON_UNCONFIRMED");
            int role=(int)roleValue,state=(int)stateValue;
            int left,top,width,height;
            accessible.accLocation(out left,out top,out width,out height,child);
            System.Drawing.Rectangle button=new System.Drawing.Rectangle(left,top,width,height);
            System.Drawing.Rectangle icon=new System.Drawing.Rectangle(toolbar.Left+match.X,toolbar.Top+match.Y,match.Width,match.Height);
            if(role!=43 || (state & (1|32768|65536))!=0 || width<=0 || height<=0 ||
               !toolbar.Contains(button) || !button.Contains(icon))
                throw new System.Exception("ICON_BUTTON_UNCONFIRMED");
            return new int[] { left,top,left+width,top+height,state,role };
        } finally {
            if(accessible!=null && System.Runtime.InteropServices.Marshal.IsComObject(accessible))
                System.Runtime.InteropServices.Marshal.ReleaseComObject(accessible);
            if(child!=null && System.Runtime.InteropServices.Marshal.IsComObject(child))
                System.Runtime.InteropServices.Marshal.ReleaseComObject(child);
        }
    }
    public static Output Run(long rootId,long processId,long toolbarId,string expected,string templatePath) {
        SetProcessDPIAware();
        System.Drawing.Rectangle rect=Validate(rootId,processId,toolbarId,expected);
        System.IntPtr root=new System.IntPtr(rootId);SetForegroundWindow(root);
        for(int i=0;i<50 && GetForegroundWindow()!=root;i++) System.Threading.Thread.Sleep(20);
        if(GetForegroundWindow()!=root) throw new System.Exception("ICON_FOREGROUND_UNCONFIRMED");
        using(System.Drawing.Bitmap image=new System.Drawing.Bitmap(rect.Width,rect.Height,System.Drawing.Imaging.PixelFormat.Format32bppArgb))
        using(System.Drawing.Bitmap template=new System.Drawing.Bitmap(templatePath)) {
            using(System.Drawing.Graphics g=System.Drawing.Graphics.FromImage(image))
                g.CopyFromScreen(rect.Location,System.Drawing.Point.Empty,rect.Size,System.Drawing.CopyPixelOperation.SourceCopy);
            Validate(rootId,processId,toolbarId,expected);
            if(GetForegroundWindow()!=root) throw new System.Exception("ICON_FOREGROUND_UNCONFIRMED");
            WdmsToolbarIconMatcher.Match result=WdmsToolbarIconMatcher.Find(image,template);
            int[] button=new int[] { 0,0,0,0,-1,-1 };
            if(result.Score>=.86 && result.Score-result.RunnerUp>=.06)
                button=Button(rect,rootId,toolbarId,result);
            Validate(rootId,processId,toolbarId,expected);
            return new Output { Match=result,Button=button };
        }
    }
}
'@
    $phase = 'compile'
    Add-Type -TypeDefinition ($matcher + [Environment]::NewLine + $capture) -Language CSharp -ReferencedAssemblies @($system.Location,$drawing.Location,$forms.Location,$accessibility.Location)
    $phase = 'capture_match'
    $values = [WdmsToolbarIconCapture]::Run($RootHwnd,$ProcessId,$ContainerHwnd,$ExpectedRect,$TemplateFile)
    $match = $values.Match
    $button = $values.Button
    Write-Result @{
        revision=[WdmsToolbarIconMatcher]::Revision; template_id=18
        rect=@($match.X,$match.Y,($match.X+$match.Width),($match.Y+$match.Height))
        score=$match.Score; runner_up=$match.RunnerUp; scale_height=$match.ScaleHeight
        elapsed_ms=$match.ElapsedMs; button_rect=@($button[0],$button[1],$button[2],$button[3])
        button_state=$button[4]; button_role=$button[5]
    }
} catch {
    $code = 'ICON_UNAVAILABLE'
    if ($phase -eq 'compile') { $code = 'ICON_BRIDGE_COMPILE_FAILED' }
    foreach ($known in @('ICON_CONTAINER_CHANGED','ICON_FOREGROUND_UNCONFIRMED','ICON_IMAGE_INVALID',
                         'ICON_MATCH_TIMEOUT','ICON_TARGET_NOT_FOUND','ICON_BUTTON_UNCONFIRMED','ICON_TARGET_OBSCURED')) {
        if ($_ -match $known) { $code = $known; break }
    }
    Write-Result @{ error_code=$code; stage=$phase }
}
