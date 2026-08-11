// COM registration pattern adapted from Microsoft's WindowsAppSDK-Samples (MIT).
using System.Runtime.InteropServices;
using Microsoft.Windows.Widgets.Providers;
using WinRT;

namespace BjtuHpc.WidgetProvider;

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("00000001-0000-0000-C000-000000000046")]
internal interface IClassFactory
{
    [PreserveSig] int CreateInstance(IntPtr outer, ref Guid iid, out IntPtr instance);
    [PreserveSig] int LockServer(bool locked);
}

internal static class ComClassObject
{
    public static IDisposable Register(Guid classId, IClassFactory factory)
    {
        var result = CoRegisterClassObject(classId, factory, 0x4, 0x1, out var cookie);
        if (result != 0) Marshal.ThrowExceptionForHR(result);
        return new Revoke(cookie);
    }

    private sealed class Revoke(uint cookie) : IDisposable
    {
        public void Dispose() => CoRevokeClassObject(cookie);
    }

    [DllImport("ole32.dll")]
    private static extern int CoRegisterClassObject(
        [MarshalAs(UnmanagedType.LPStruct)] Guid classId,
        [MarshalAs(UnmanagedType.IUnknown)] object factory,
        uint context, uint flags, out uint cookie);

    [DllImport("ole32.dll")]
    private static extern int CoRevokeClassObject(uint cookie);
}

internal sealed class WidgetProviderFactory<T> : IClassFactory where T : IWidgetProvider, new()
{
    public int CreateInstance(IntPtr outer, ref Guid iid, out IntPtr instance)
    {
        instance = IntPtr.Zero;
        if (outer != IntPtr.Zero) Marshal.ThrowExceptionForHR(unchecked((int)0x80040110));
        if (iid != typeof(T).GUID && iid != new Guid("00000000-0000-0000-C000-000000000046"))
            Marshal.ThrowExceptionForHR(unchecked((int)0x80004002));
        instance = MarshalInspectable<IWidgetProvider>.FromManaged(new T());
        return 0;
    }

    public int LockServer(bool locked) => 0;
}
