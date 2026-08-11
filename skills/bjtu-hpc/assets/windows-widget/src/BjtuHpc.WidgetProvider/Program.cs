using Microsoft.Windows.Widgets.Providers;

namespace BjtuHpc.WidgetProvider;

public static class Program
{
    [MTAThread]
    public static void Main(string[] args)
    {
        if (!args.Contains("-RegisterProcessAsComServer", StringComparer.OrdinalIgnoreCase)) return;
        WinRT.ComWrappersSupport.InitializeComWrappers();
        using var manager = RegistrationManager<WidgetProvider>.RegisterProvider();
        using var disposedEvent = manager.GetDisposedEvent();
        disposedEvent.WaitOne();
    }
}
