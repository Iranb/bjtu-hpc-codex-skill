using System.Configuration;
using System.Data;
using System.Windows;

namespace BjtuHpc.Desktop;

/// <summary>
/// Interaction logic for App.xaml
/// </summary>
public partial class App : System.Windows.Application
{
    private MainWindow? _window;
    private TrayIconHost? _trayIcon;
    private bool _isExiting;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        _window = new MainWindow();
        MainWindow = _window;
        _trayIcon = new TrayIconHost(
            showWindow: ShowWidget,
            reloadSnapshot: () => _window.ReloadFromTrayAsync(),
            openDashboard: _window.OpenDashboardFromTray,
            exitApplication: ExitApplication);

        _window.Show();
    }

    private void ShowWidget()
    {
        if (_window is null) return;
        _window.ShowFromTray();
    }

    private void ExitApplication()
    {
        if (_isExiting) return;
        _isExiting = true;
        _trayIcon?.Dispose();
        _trayIcon = null;
        _window?.CloseForExit();
        Shutdown();
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _trayIcon?.Dispose();
        base.OnExit(e);
    }
}
