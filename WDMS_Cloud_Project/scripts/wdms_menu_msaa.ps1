param(
    [Parameter(Mandatory=$true)][string]$RequestFile,
    [Parameter(Mandatory=$true)][string]$OutputFile
)

$ErrorActionPreference = 'Stop'
$phase = 'request_path'
$outputPath = $null

function Write-SafeFailure([string]$stage) {
    if (-not $outputPath) { return }
    try {
        $record = @{ event = 'menu_msaa_error'; stage = $stage; error_type = 'PowerShellError' } | ConvertTo-Json -Compress
        $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
        [System.IO.File]::AppendAllText($outputPath, $record + [Environment]::NewLine, $utf8NoBom)
    }
    catch { }
}

try {
    $requestPath = [System.IO.Path]::GetFullPath($RequestFile)
    $outputPath = [System.IO.Path]::GetFullPath($OutputFile)
    if (-not [System.IO.File]::Exists($requestPath)) { throw 'request file unavailable' }

    $phase = 'request_read_and_validate'
    $utf8Strict = [System.Text.UTF8Encoding]::new($false, $true)
    $requestJson = [System.IO.File]::ReadAllText($requestPath, $utf8Strict)
    $request = $requestJson | ConvertFrom-Json -ErrorAction Stop
    if ($null -eq $request -or $request -isnot [pscustomobject]) { throw 'invalid request' }
    if ($request.schema_version -isnot [int] -or $request.schema_version -ne 1) { throw 'invalid request' }
    if ($request.root_hwnd -isnot [ValueType] -or $request.pid -isnot [ValueType]) { throw 'invalid request' }
    if ($request.containers -isnot [System.Array] -or $request.containers.Count -lt 1 -or $request.containers.Count -gt 16) { throw 'invalid request' }
    foreach ($container in $request.containers) {
        if ($null -eq $container -or $container -isnot [pscustomobject]) { throw 'invalid request' }
        if ($container.class_name -isnot [string] -or -not $container.class_name.StartsWith('WindowsForms10.Window.8.', [System.StringComparison]::Ordinal)) { throw 'invalid request' }
        if ($container.rect -isnot [System.Array] -or $container.rect.Count -ne 4) { throw 'invalid request' }
        if ($container.depth -isnot [int] -or $container.depth -lt 1 -or $container.depth -gt 3) { throw 'invalid request' }
    }

    $phase = 'assembly_load'
    $accessibility = [System.Reflection.Assembly]::Load('Accessibility, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a')
    $webExtensions = [System.Reflection.Assembly]::Load('System.Web.Extensions, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35')
    $system = [System.Reflection.Assembly]::Load('System, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089')
    $systemCore = [System.Reflection.Assembly]::Load('System.Core, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089')

    $source = @'
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using Accessibility;

public static class WdmsMenuMsaaProbe
{
    private const int MaxNodes = 128;
    private const int MaxDepth = 4;
    private const int MaxChildren = 32;
    private const int MaxMilliseconds = 5000;
    private const uint ObjIdClient = 0xFFFFFFFC;
    private static readonly Guid IidIAccessible = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");
    private static readonly HashSet<int> AllowedRoles = new HashSet<int> { 10, 22, 2, 11, 12, 43, 30, 20, 16 };
    private static readonly HashSet<string> Targets = new HashSet<string>(StringComparer.Ordinal)
        { "在库管理", "在库查询", "在库商品检索", "Tsp_顶部子系统菜单" };

    private static StreamWriter writer;
    private static Stopwatch clock;
    private static string stage;
    private static int nodeCount;
    private static int completedContainers;
    private static List<Dictionary<string, object>> matches;
    private static Dictionary<string, int> containerRoles;
    private static int containerRoleUnavailable;
    private static int containerFiltered;
    private static int containerObjects;
    private static int containerElements;
    private static int containerWindowWrappers;
    private static int containerClientWrappers;
    private static int containerNodeStart;
    private static int containerNodeBudget;
    private static int nodePrunedContainers;
    private static int? containerRootRole;
    private static int? containerRootChildCount;
    private static long activeContainerHwnd;
    private static int? depthLimitRole;
    private static int? depthLimitDepth;
    private static int? depthLimitChildCount;
    private static List<int> depthLimitPath;
    private static string depthLimitPathKind;
    private static int depthPrunedBranches;

    private sealed class ProbeFailure : Exception
    {
        public readonly string Phase;
        public readonly int HResultValue;
        public ProbeFailure(string phase, int hresult) { Phase = phase; HResultValue = hresult; }
        public ProbeFailure(string phase) { Phase = phase; HResultValue = 0; }
    }

    private sealed class ProbeLimit : Exception
    {
        public readonly string Kind;
        public ProbeLimit(string kind) { Kind = kind; }
    }

    private sealed class ContainerNodeLimit : Exception { }

    private sealed class ContainerRequest
    {
        public long Hwnd;
        public string ClassName;
        public int Depth;
    }

    private sealed class Request
    {
        public int SchemaVersion;
        public long RootHwnd;
        public long Pid;
        public List<ContainerRequest> Containers;
    }

    public static int Run(string requestFile, string outputFile)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(outputFile));
        using (writer = new StreamWriter(outputFile, false, new UTF8Encoding(false)))
        {
            writer.AutoFlush = true;
            clock = Stopwatch.StartNew();
            nodeCount = 0;
            completedContainers = 0;
            depthPrunedBranches = 0;
            nodePrunedContainers = 0;
            matches = new List<Dictionary<string, object>>();
            try
            {
                stage = "request_read";
                Request request = ReadRequest(requestFile);
                stage = "root_identity";
                ValidateRoot(request);

                foreach (ContainerRequest container in request.Containers)
                {
                    containerNodeStart = nodeCount;
                    // Reserve a share for every remaining candidate. Unused shares
                    // carry forward, while the global 128-node limit stays intact.
                    containerNodeBudget = (MaxNodes - nodeCount) /
                        (request.Containers.Count - completedContainers);
                    bool containerTruncated = false;
                    activeContainerHwnd = container.Hwnd;
                    depthLimitRole = null;
                    depthLimitDepth = null;
                    depthLimitChildCount = null;
                    depthLimitPath = null;
                    depthLimitPathKind = null;
                    CheckBudget();
                    stage = "container_identity";
                    ValidateContainer(request, container);
                    int before = matches.Count;
                    Write("menu_msaa_container_started", new Dictionary<string, object>
                    {
                        { "container_hwnd", container.Hwnd }
                    });

                    IAccessible accessible = null;
                    try
                    {
                        stage = "AccessibleObjectFromWindow";
                        CheckBudget();
                        Guid iid = IidIAccessible;
                        int hr = Native.AccessibleObjectFromWindow(new IntPtr(container.Hwnd), ObjIdClient,
                            ref iid, out accessible);
                        CheckBudget();
                        if (hr != 0 || accessible == null) throw new ProbeFailure(stage, hr);
                        containerRoles = new Dictionary<string, int>();
                        containerRoleUnavailable = 0;
                        containerFiltered = 0;
                        containerObjects = 0;
                        containerElements = 0;
                        containerWindowWrappers = 0;
                        containerClientWrappers = 0;
                        containerRootRole = null;
                        containerRootChildCount = null;
                        VisitObject(accessible, request, container.Hwnd, 0,
                            new List<int>(), "child_id");
                    }
                    catch (ContainerNodeLimit)
                    {
                        containerTruncated = true;
                        nodePrunedContainers++;
                        Write("menu_msaa_container_pruned", new Dictionary<string, object>
                        {
                            { "kind", "container_node_count" },
                            { "container_hwnd", container.Hwnd },
                            { "node_budget", containerNodeBudget },
                            { "nodes", nodeCount - containerNodeStart }
                        });
                    }
                    finally
                    {
                        ReleaseCom(accessible);
                    }

                    completedContainers++;
                    Write("menu_msaa_container_complete", new Dictionary<string, object>
                    {
                        { "container_hwnd", container.Hwnd },
                        { "matches_count", matches.Count - before },
                        { "probe_revision", "role-summary-v3-client-wrapper" },
                        { "roles", containerRoles },
                        { "role_unavailable", containerRoleUnavailable },
                        { "filtered_nodes", containerFiltered },
                        { "object_nodes", containerObjects },
                        { "element_nodes", containerElements },
                        { "window_wrappers", containerWindowWrappers },
                        { "client_wrappers", containerClientWrappers },
                        { "truncated", containerTruncated },
                        { "node_budget", containerNodeBudget },
                        { "root_role", containerRootRole },
                        { "root_child_count", containerRootChildCount }
                    });
                }

                if (depthPrunedBranches > 0 || nodePrunedContainers > 0)
                {
                    Write("menu_msaa_partial", new Dictionary<string, object>
                    {
                        { "probe_revision", "fair-coverage-v5" },
                        { "kind", "coverage" }, { "truncated", true },
                        { "branches_pruned", depthPrunedBranches },
                        { "containers_pruned", nodePrunedContainers },
                        { "containers_scanned", completedContainers },
                        { "nodes", nodeCount }, { "elapsed_ms", clock.ElapsedMilliseconds },
                        { "matches_so_far", matches }
                    });
                    return 2;
                }
                Write("menu_msaa_complete", new Dictionary<string, object>
                {
                    { "schema_version", 1 },
                    { "matches", matches },
                    { "nodes", nodeCount },
                    { "elapsed_ms", clock.ElapsedMilliseconds },
                    { "containers_completed", completedContainers },
                    { "truncated", false }
                });
                return 0;
            }
            catch (ProbeLimit limit)
            {
                Write("menu_msaa_limit", new Dictionary<string, object>
                {
                    { "kind", limit.Kind },
                    { "stage", stage },
                    { "nodes", nodeCount },
                    { "elapsed_ms", clock.ElapsedMilliseconds },
                    { "context_revision", "depth-context-v1" },
                    { "container_hwnd", activeContainerHwnd },
                    { "limit_role", depthLimitRole },
                    { "limit_depth", depthLimitDepth },
                    { "limit_child_count", depthLimitChildCount },
                    { "limit_path", depthLimitPath },
                    { "limit_path_kind", depthLimitPathKind },
                    { "roles", containerRoles },
                    { "role_unavailable", containerRoleUnavailable },
                    { "filtered_nodes", containerFiltered },
                    { "object_nodes", containerObjects },
                    { "element_nodes", containerElements },
                    { "window_wrappers", containerWindowWrappers },
                    { "matches_so_far", matches }
                });
                return 2;
            }
            catch (ProbeFailure failure)
            {
                Write("menu_msaa_error", new Dictionary<string, object>
                {
                    { "stage", failure.Phase },
                    { "hresult", failure.HResultValue }
                });
                return 2;
            }
            catch (COMException error)
            {
                Write("menu_msaa_error", new Dictionary<string, object>
                {
                    { "stage", stage },
                    { "hresult", error.HResult }
                });
                return 2;
            }
            catch (Exception error)
            {
                Write("menu_msaa_error", new Dictionary<string, object>
                {
                    { "stage", stage },
                    { "error_type", error.GetType().Name }
                });
                return 2;
            }
        }
    }

    private static Request ReadRequest(string requestFile)
    {
        string json = File.ReadAllText(requestFile, new UTF8Encoding(false, true));
        JavaScriptSerializer serializer = new JavaScriptSerializer();
        serializer.MaxJsonLength = 65536;
        object raw = serializer.DeserializeObject(json);
        Dictionary<string, object> root = AsDictionary(raw, "request_read");
        RequireKeys(root, new string[] { "schema_version", "root_hwnd", "pid", "containers" }, "request_read");

        Request request = new Request();
        request.SchemaVersion = CheckedInt(root["schema_version"], "request_read");
        request.RootHwnd = CheckedLong(root["root_hwnd"], "request_read");
        request.Pid = CheckedLong(root["pid"], "request_read");
        if (request.SchemaVersion != 1 || request.RootHwnd <= 0 || request.Pid <= 0)
            throw new ProbeFailure("request_read");

        IList rawContainers = root["containers"] as IList;
        if (rawContainers == null || rawContainers.Count < 1 || rawContainers.Count > 16)
            throw new ProbeFailure("request_read");
        request.Containers = new List<ContainerRequest>();
        HashSet<long> seen = new HashSet<long>();
        foreach (object rawContainer in rawContainers)
        {
            Dictionary<string, object> item = AsDictionary(rawContainer, "request_read");
            RequireKeys(item, new string[] { "hwnd", "class_name", "rect", "depth" }, "request_read");
            ContainerRequest candidate = new ContainerRequest();
            candidate.Hwnd = CheckedLong(item["hwnd"], "request_read");
            candidate.ClassName = item["class_name"] as string;
            candidate.Depth = CheckedInt(item["depth"], "request_read");
            IList rect = item["rect"] as IList;
            if (candidate.Hwnd <= 0 || candidate.Hwnd == request.RootHwnd
                || !seen.Add(candidate.Hwnd) || candidate.ClassName == null
                || !candidate.ClassName.StartsWith("WindowsForms10.Window.8.", StringComparison.Ordinal)
                || candidate.Depth < 1 || candidate.Depth > 3 || rect == null || rect.Count != 4)
                throw new ProbeFailure("request_read");
            for (int i = 0; i < rect.Count; i++) CheckedInt(rect[i], "request_read");
            request.Containers.Add(candidate);
        }
        return request;
    }

    private static Dictionary<string, object> AsDictionary(object value, string phase)
    {
        Dictionary<string, object> result = value as Dictionary<string, object>;
        if (result == null) throw new ProbeFailure(phase);
        return result;
    }

    private static void RequireKeys(Dictionary<string, object> item, string[] names, string phase)
    {
        if (item.Count != names.Length) throw new ProbeFailure(phase);
        foreach (string name in names) if (!item.ContainsKey(name)) throw new ProbeFailure(phase);
    }

    private static long CheckedLong(object value, string phase)
    {
        if (value == null || value is bool) throw new ProbeFailure(phase);
        try
        {
            if (value is int || value is long || value is short || value is byte || value is uint)
                return Convert.ToInt64(value, CultureInfo.InvariantCulture);
        }
        catch { }
        throw new ProbeFailure(phase);
    }

    private static int CheckedInt(object value, string phase)
    {
        long number = CheckedLong(value, phase);
        if (number < Int32.MinValue || number > Int32.MaxValue) throw new ProbeFailure(phase);
        return (int)number;
    }

    private static void ValidateRoot(Request request)
    {
        IntPtr root = new IntPtr(request.RootHwnd);
        if (!Native.IsWindow(root) || !Native.IsWindowVisible(root) || Native.IsIconic(root))
            throw new ProbeFailure("root_identity");
        uint pid;
        Native.GetWindowThreadProcessId(root, out pid);
        if (pid != request.Pid) throw new ProbeFailure("root_identity");
    }

    private static void ValidateContainer(Request request, ContainerRequest container)
    {
        IntPtr root = new IntPtr(request.RootHwnd);
        IntPtr hwnd = new IntPtr(container.Hwnd);
        if (!Native.IsWindow(hwnd) || !Native.IsWindowVisible(hwnd) || !Native.IsChild(root, hwnd))
            throw new ProbeFailure("container_identity");
        uint pid;
        Native.GetWindowThreadProcessId(hwnd, out pid);
        if (pid != request.Pid) throw new ProbeFailure("container_identity");
        StringBuilder className = new StringBuilder(256);
        if (Native.GetClassName(hwnd, className, className.Capacity) <= 0
            || !String.Equals(className.ToString(), container.ClassName, StringComparison.Ordinal))
            throw new ProbeFailure("container_identity");
    }

    private static void VisitObject(IAccessible accessible, Request request, long containerHwnd,
        int depth, List<int> path, string pathKind)
    {
        CountNode();
        containerObjects++;
        object self = 0;
        int role;
        if (!ReadRole(accessible, self, out role))
        {
            WriteNode(containerHwnd, depth, path, pathKind, "object", null);
            containerRoleUnavailable++;
            return;
        }
        WriteNode(containerHwnd, depth, path, pathKind, "object", role);
        string roleKey = role.ToString(CultureInfo.InvariantCulture);
        if (containerRoles.ContainsKey(roleKey)) containerRoles[roleKey]++;
        else containerRoles.Add(roleKey, 1);
        if (depth == 0 && path.Count == 0) containerRootRole = role;
        if (role == 9)
        {
            containerWindowWrappers++;
            EnumerateObjectChildren(accessible, request, containerHwnd, depth, path, pathKind, role);
            return;
        }
        if (role == 4)
        {
            // CLIENT is a structural host, not a business text target.
            // Traverse children without reading this wrapper's name or state.
            containerClientWrappers++;
            EnumerateObjectChildren(accessible, request, containerHwnd, depth, path, pathKind, role);
            return;
        }
        if (!AllowedRoles.Contains(role))
        {
            containerFiltered++;
            return;
        }
        string name = ReadName(accessible, self);
        int state = ReadState(accessible, self);
        AddMatch(name, containerHwnd, path, pathKind, role, state);
        EnumerateObjectChildren(accessible, request, containerHwnd, depth, path, pathKind, role);
    }

    private static void VisitChildElement(IAccessible parent, int childId, long containerHwnd,
        int depth, List<int> path, string pathKind)
    {
        CountNode();
        containerElements++;
        object id = childId;
        int role;
        if (!ReadRole(parent, id, out role))
        {
            WriteNode(containerHwnd, depth, path, pathKind, "element", null);
            containerRoleUnavailable++;
            return;
        }
        WriteNode(containerHwnd, depth, path, pathKind, "element", role);
        string roleKey = role.ToString(CultureInfo.InvariantCulture);
        if (containerRoles.ContainsKey(roleKey)) containerRoles[roleKey]++;
        else containerRoles.Add(roleKey, 1);
        if (!AllowedRoles.Contains(role))
        {
            containerFiltered++;
            return;
        }
        string name = ReadName(parent, id);
        int state = ReadState(parent, id);
        AddMatch(name, containerHwnd, path, pathKind, role, state);
        // A returned child ID is an element of its parent, not an IAccessible
        // object with descendants. AccessibleChildren returns an object for
        // nodes that can be recursively traversed.
    }

    private static void EnumerateObjectChildren(IAccessible accessible, Request request,
        long containerHwnd, int depth, List<int> path, string pathKind, int objectRole)
    {
        stage = "accChildCount";
        CheckBudget();
        int count = accessible.accChildCount;
        CheckBudget();
        if (depth == 0 && path.Count == 0) containerRootChildCount = count;
        if (count < 0) throw new ProbeFailure(stage);
        if (count > MaxChildren) throw new ProbeLimit("container_child_count");
        if (count == 0) return;
        if (depth >= MaxDepth)
        {
            depthLimitRole = objectRole;
            depthLimitDepth = depth;
            depthLimitChildCount = count;
            depthLimitPath = new List<int>(path);
            depthLimitPathKind = pathKind;
            depthPrunedBranches++;
            Write("menu_msaa_branch_pruned", new Dictionary<string, object>
            {
                { "kind", "depth" }, { "container_hwnd", containerHwnd },
                { "role", objectRole }, { "depth", depth },
                { "child_count", count }, { "child_path", new List<int>(path) },
                { "path_kind", pathKind }
            });
            return;
        }

        object[] children = new object[count];
        try
        {
            int obtained = 0;
            stage = "AccessibleChildren";
            CheckBudget();
            int hr = Native.AccessibleChildren(accessible, 0, count, children, out obtained);
            CheckBudget();
            if (obtained < 0 || obtained > count) throw new ProbeFailure(stage, hr);
            if (hr != 0 && hr != 1) throw new ProbeFailure(stage, hr);
            if (hr == 1 || obtained != count)
            {
                Write("menu_msaa_partial", new Dictionary<string, object>
                {
                    { "stage", stage }, { "requested", count }, { "obtained", obtained }, { "hresult", hr }
                });
                throw new ProbeFailure(stage, hr);
            }

            for (int index = 0; index < obtained; index++)
            {
                CheckBudget();
                object item = children[index];
                if (item is int)
                {
                    int childId = (int)item;
                    if (childId < 0) throw new ProbeFailure("AccessibleChildren_item_kind");
                    List<int> childPath = Append(path, childId);
                    VisitChildElement(accessible, childId, containerHwnd, depth + 1, childPath, pathKind);
                }
                else if (item is IAccessible)
                {
                    IAccessible child = (IAccessible)item;
                    List<int> childPath = Append(path, index);
                    VisitObject(child, request, containerHwnd, depth + 1,
                        childPath, "index_or_child_id");
                }
                else
                {
                    throw new ProbeFailure("AccessibleChildren_item_kind");
                }
            }
        }
        finally
        {
            // Own all returned references, including unvisited entries on failure.
            foreach (object item in children) ReleaseCom(item);
        }
    }

    private static List<int> Append(List<int> path, int value)
    {
        List<int> result = new List<int>(path);
        result.Add(value);
        return result;
    }

    private static bool ReadRole(IAccessible accessible, object childId, out int role)
    {
        stage = "get_accRole";
        CheckBudget();
        object value = accessible.get_accRole(childId);
        CheckBudget();
        if (!(value is int))
        {
            role = 0;
            return false;
        }
        role = (int)value;
        return true;
    }

    private static string ReadName(IAccessible accessible, object childId)
    {
        stage = "get_accName";
        CheckBudget();
        string value = accessible.get_accName(childId);
        CheckBudget();
        return (value ?? String.Empty).Replace("&", String.Empty).Trim();
    }

    private static int ReadState(IAccessible accessible, object childId)
    {
        stage = "get_accState";
        CheckBudget();
        object value = accessible.get_accState(childId);
        CheckBudget();
        return CheckedInt(value, stage);
    }

    private static void AddMatch(string name, long containerHwnd, List<int> path,
        string pathKind, int role, int state)
    {
        if (!Targets.Contains(name)) return;
        matches.Add(new Dictionary<string, object>
        {
            { "target", name },
            { "container_hwnd", containerHwnd },
            { "child_path", new List<int>(path) },
            { "path_kind", pathKind },
            { "role", role },
            { "state", state }
        });
    }

    private static void CountNode()
    {
        CheckBudget();
        if (nodeCount - containerNodeStart >= containerNodeBudget)
            throw new ContainerNodeLimit();
        if (nodeCount >= MaxNodes) throw new ProbeLimit("node_count");
        nodeCount++;
    }

    private static void CheckBudget()
    {
        if (clock.ElapsedMilliseconds >= MaxMilliseconds) throw new ProbeLimit("time");
    }

    private static void ReleaseCom(object value)
    {
        if (value == null) return;
        try { if (Marshal.IsComObject(value)) Marshal.ReleaseComObject(value); }
        catch { }
    }

    private static void Write(string eventName, Dictionary<string, object> fields)
    {
        Dictionary<string, object> item = new Dictionary<string, object>();
        item.Add("event", eventName);
        foreach (KeyValuePair<string, object> field in fields) item[field.Key] = field.Value;
        writer.WriteLine(new JavaScriptSerializer().Serialize(item));
        writer.Flush();
    }

    private static void WriteNode(long containerHwnd, int depth, List<int> path,
        string pathKind, string nodeKind, int? role)
    {
        // Structural evidence only; no extra COM calls or arbitrary UI text.
        Write("menu_msaa_node", new Dictionary<string, object>
        {
            { "node", nodeCount }, { "container_hwnd", containerHwnd },
            { "depth", depth }, { "child_path", new List<int>(path) },
            { "path_kind", pathKind }, { "node_kind", nodeKind }, { "role", role }
        });
    }

    private static class Native
    {
        [DllImport("oleacc.dll", PreserveSig = true)]
        internal static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint objectId,
            ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out IAccessible accessible);

        [DllImport("oleacc.dll", PreserveSig = true)]
        internal static extern int AccessibleChildren(
            [MarshalAs(UnmanagedType.Interface)] IAccessible container,
            int childStart, int childCount,
            [Out, MarshalAs(UnmanagedType.LPArray, ArraySubType = UnmanagedType.Struct, SizeParamIndex = 2)] object[] children,
            out int obtained);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool IsWindow(IntPtr hwnd);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool IsWindowVisible(IntPtr hwnd);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool IsIconic(IntPtr hwnd);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool IsChild(IntPtr parent, IntPtr child);

        [DllImport("user32.dll", SetLastError = true)]
        internal static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern int GetClassName(IntPtr hwnd, StringBuilder className, int maxCount);
    }
}
'@

    $phase = 'add_type_compilation'
    Add-Type -TypeDefinition $source -ReferencedAssemblies @($system.Location, $systemCore.Location, $accessibility.Location, $webExtensions.Location) -Language CSharp
    $phase = 'msaa_runner'
    $result = [WdmsMenuMsaaProbe]::Run($requestPath, $outputPath)
    exit $result
}
catch {
    Write-SafeFailure $phase
    exit 2
}
