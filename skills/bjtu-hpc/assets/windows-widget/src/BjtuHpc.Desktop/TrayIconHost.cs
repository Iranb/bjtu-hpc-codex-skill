using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using Forms = System.Windows.Forms;

namespace BjtuHpc.Desktop;

internal sealed class TrayIconHost : IDisposable
{
    private readonly Forms.NotifyIcon _notifyIcon;
    private readonly Forms.ContextMenuStrip _menu;
    private readonly Icon _icon;
    private bool _disposed;

    public TrayIconHost(
        Action showWindow,
        Func<Task> reloadSnapshot,
        Action openDashboard,
        Action exitApplication)
    {
        ArgumentNullException.ThrowIfNull(showWindow);
        ArgumentNullException.ThrowIfNull(reloadSnapshot);
        ArgumentNullException.ThrowIfNull(openDashboard);
        ArgumentNullException.ThrowIfNull(exitApplication);

        _icon = LoadTrayIcon();
        _menu = new Forms.ContextMenuStrip();
        _menu.Items.Add("Show widget", null, (_, _) => Dispatch(showWindow));
        _menu.Items.Add("Reload snapshot", null, (_, _) => DispatchAsync(reloadSnapshot));
        _menu.Items.Add("Open dashboard", null, (_, _) => Dispatch(openDashboard));
        _menu.Items.Add(new Forms.ToolStripSeparator());
        _menu.Items.Add("Exit", null, (_, _) => Dispatch(exitApplication));

        _notifyIcon = new Forms.NotifyIcon
        {
            Text = "BJTU HPC Widget",
            Icon = _icon,
            ContextMenuStrip = _menu,
            Visible = true
        };
        _notifyIcon.DoubleClick += (_, _) => Dispatch(showWindow);
    }

    private static void Dispatch(Action action) =>
        System.Windows.Application.Current.Dispatcher.BeginInvoke(action);

    private static void DispatchAsync(Func<Task> action) =>
        System.Windows.Application.Current.Dispatcher.BeginInvoke(async () => await action());

    private static Icon LoadTrayIcon()
    {
        var imagePath = Path.Combine(AppContext.BaseDirectory, "Assets", "TrayLogo.png");
        if (!File.Exists(imagePath)) return (Icon)SystemIcons.Application.Clone();

        using var bitmap = new Bitmap(imagePath);
        var handle = bitmap.GetHicon();
        try
        {
            return (Icon)Icon.FromHandle(handle).Clone();
        }
        finally
        {
            DestroyIcon(handle);
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _notifyIcon.Visible = false;
        _notifyIcon.Dispose();
        _menu.Dispose();
        _icon.Dispose();
    }

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DestroyIcon(IntPtr handle);
}
