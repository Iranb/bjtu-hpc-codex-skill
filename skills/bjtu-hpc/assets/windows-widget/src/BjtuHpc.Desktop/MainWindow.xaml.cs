using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Net.Http.Json;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Threading;
using BjtuHpc.Widget.Core;
using Button = System.Windows.Controls.Button;

namespace BjtuHpc.Desktop;

public partial class MainWindow : Window, INotifyPropertyChanged
{
    private const int AccountsPerPage = 3;
    private readonly SnapshotStore _store = new();
    private readonly string _snapshotPath;
    private readonly string _configPath = Path.Combine(SnapshotStore.DefaultDirectory, "config.json");
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromSeconds(60) };
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(8) };
    private FileSystemWatcher? _watcher;
    private IReadOnlyList<AccountDisplay> _allAccounts = [];
    private int _page;
    private int _gpuFree;
    private int _gpuTotal;
    private int _cpuFree;
    private int _cpuTotal;
    private int _running;
    private int _pending;
    private int _attention;
    private string _statusText = "Waiting for a redacted queue snapshot.";
    private string _updatedText = "Not updated";
    private bool _isStale = true;
    private bool _hasError;
    private bool _closeForExit;

    public MainWindow()
    {
        InitializeComponent();
        DataContext = this;
        _snapshotPath = Environment.GetEnvironmentVariable("HPC_WIDGET_SNAPSHOT") ?? SnapshotStore.DefaultPath;
        LoadConfig();
        _timer.Tick += async (_, _) => await ReloadAsync();
        _timer.Start();
        ConfigureWatcher();
        Loaded += async (_, _) => await ReloadAsync();
        Closed += (_, _) =>
        {
            _timer.Stop();
            _watcher?.Dispose();
            _http.Dispose();
            SaveConfig();
        };
    }

    public ObservableCollection<AccountDisplay> VisibleAccounts { get; } = [];
    public ObservableCollection<NodeDisplay> VisibleNodes { get; } = [];
    public int GpuFree
    {
        get => _gpuFree;
        private set
        {
            Set(ref _gpuFree, value);
            OnPropertyChanged(nameof(GpuPercentText));
        }
    }
    public int GpuTotal
    {
        get => _gpuTotal;
        private set
        {
            Set(ref _gpuTotal, value);
            OnPropertyChanged(nameof(GpuTotalSafe));
            OnPropertyChanged(nameof(GpuPercentText));
        }
    }
    public int GpuTotalSafe => Math.Max(1, GpuTotal);
    public string GpuPercentText => GpuTotal > 0
        ? $"{Math.Round(GpuFree * 100d / GpuTotal):0}% free"
        : "No capacity data";
    public int Running { get => _running; private set => Set(ref _running, value); }
    public int Pending { get => _pending; private set => Set(ref _pending, value); }
    public string CpuText => $"CPU  {_cpuFree} / {_cpuTotal}";
    public string CpuCapacityText => $"{_cpuFree} / {_cpuTotal}";
    public string AccountText => $"{_allAccounts.Count} total  \u00B7  {_attention} attention";
    public string HealthLabel => HasError ? "OFFLINE" : IsStale ? "STALE" : _attention > 0 ? "ATTENTION" : "HEALTHY";
    public string StatusText { get => _statusText; private set => Set(ref _statusText, value); }
    public string UpdatedText { get => _updatedText; private set => Set(ref _updatedText, value); }
    public bool IsStale
    {
        get => _isStale;
        private set
        {
            Set(ref _isStale, value);
            OnPropertyChanged(nameof(HealthLabel));
        }
    }
    public bool HasError
    {
        get => _hasError;
        private set
        {
            Set(ref _hasError, value);
            OnPropertyChanged(nameof(HealthLabel));
        }
    }
    public string PageText => _allAccounts.Count == 0 ? "0 / 0" : $"{_page + 1} / {PageCount}";
    private int PageCount => Math.Max(1, (int)Math.Ceiling(_allAccounts.Count / (double)AccountsPerPage));

    public event PropertyChangedEventHandler? PropertyChanged;

    private async Task ReloadAsync()
    {
        try
        {
            var demo = Environment.GetCommandLineArgs().Any(arg => arg.Equals("--demo", StringComparison.OrdinalIgnoreCase));
            var snapshot = demo ? DemoSnapshot.Create() : await _store.LoadAsync(_snapshotPath);
            Apply(SnapshotPresentation.From(snapshot, DateTimeOffset.Now));
            HasError = false;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException)
        {
            HasError = true;
            IsStale = true;
            StatusText = "Snapshot unavailable: " + error.Message;
            UpdatedText = "No trusted snapshot";
        }
    }

    private void Apply(SnapshotPresentation model)
    {
        GpuFree = model.GpuFree;
        GpuTotal = model.GpuTotal;
        _cpuFree = model.CpuFree;
        _cpuTotal = model.CpuTotal;
        Running = model.Running;
        Pending = model.Pending;
        _attention = model.AttentionCount;
        _allAccounts = model.Accounts;
        _page %= PageCount;
        VisibleNodes.ReplaceWith(model.Nodes.Take(4));
        UpdatePage();
        IsStale = model.IsStale;
        StatusText = model.IsStale
            ? "STALE \u00B7 Last trusted snapshot is older than 3 minutes."
            : model.AttentionCount > 0
                ? $"ATTENTION \u00B7 {model.AttentionCount} account(s) need login or review."
                : "HEALTHY \u00B7 Snapshot is current and all accounts are ready.";
        UpdatedText = "Updated " + model.UpdatedLabel;
        OnPropertyChanged(nameof(CpuText));
        OnPropertyChanged(nameof(CpuCapacityText));
        OnPropertyChanged(nameof(AccountText));
        OnPropertyChanged(nameof(HealthLabel));
    }

    private void UpdatePage()
    {
        VisibleAccounts.ReplaceWith(_allAccounts.Skip(_page * AccountsPerPage).Take(AccountsPerPage));
        OnPropertyChanged(nameof(PageText));
    }

    private void ConfigureWatcher()
    {
        var directory = Path.GetDirectoryName(Path.GetFullPath(_snapshotPath))!;
        Directory.CreateDirectory(directory);
        _watcher = new FileSystemWatcher(directory, Path.GetFileName(_snapshotPath))
        {
            NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite | NotifyFilters.Size,
            EnableRaisingEvents = true
        };
        FileSystemEventHandler changed = (_, _) => Dispatcher.BeginInvoke(ReloadAsync);
        RenamedEventHandler renamed = (_, _) => Dispatcher.BeginInvoke(ReloadAsync);
        _watcher.Changed += changed;
        _watcher.Created += changed;
        _watcher.Renamed += renamed;
    }

    private void Window_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ButtonState == MouseButtonState.Pressed && e.OriginalSource is not Button)
        {
            DragMove();
        }
    }

    private async void ReloadCommand_Executed(object sender, ExecutedRoutedEventArgs e)
    {
        await ReloadAsync();
        e.Handled = true;
    }

    private void CloseCommand_Executed(object sender, ExecutedRoutedEventArgs e)
    {
        HideToTray();
        e.Handled = true;
    }

    protected override void OnClosing(CancelEventArgs e)
    {
        if (!_closeForExit)
        {
            e.Cancel = true;
            HideToTray();
            return;
        }

        base.OnClosing(e);
    }

    private void HideToTray()
    {
        SaveConfig();
        Hide();
    }

    internal void ShowFromTray()
    {
        if (!IsVisible) Show();
        if (WindowState == WindowState.Minimized) WindowState = WindowState.Normal;
        Activate();
    }

    internal Task ReloadFromTrayAsync() => ReloadAsync();

    internal void OpenDashboardFromTray() => OpenUrl("http://127.0.0.1:8765/");

    internal void CloseForExit()
    {
        _closeForExit = true;
        Close();
    }

    private void TogglePinCommand_Executed(object sender, ExecutedRoutedEventArgs e)
    {
        Topmost = !Topmost;
        PinButton.Opacity = Topmost ? 1 : 0.45;
        SaveConfig();
        e.Handled = true;
    }
    private void PreviousPage_Click(object sender, RoutedEventArgs e)
    {
        _page = (_page - 1 + PageCount) % PageCount;
        UpdatePage();
    }
    private void NextPage_Click(object sender, RoutedEventArgs e)
    {
        _page = (_page + 1) % PageCount;
        UpdatePage();
    }
    private void DashboardCommand_Executed(object sender, ExecutedRoutedEventArgs e)
    {
        OpenUrl("http://127.0.0.1:8765/");
        e.Handled = true;
    }

    private async void RefreshTokensCommand_Executed(object sender, ExecutedRoutedEventArgs e)
    {
        await RequestVisibleRefreshAsync(new { accounts = "all" }, "all saved accounts");
        e.Handled = true;
    }

    private async void Login_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string alias } || string.IsNullOrWhiteSpace(alias)) return;
        await RequestVisibleRefreshAsync(new { account = alias }, alias);
    }

    private async Task RequestVisibleRefreshAsync(object selection, string label)
    {
        try
        {
            var response = await _http.PostAsJsonAsync(
                "http://127.0.0.1:8765/api/token-guardian/visible-refresh", selection);
            response.EnsureSuccessStatusCode();
            StatusText = $"Visible login requested for {label}.";
            HasError = false;
        }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException)
        {
            StatusText = "Could not reach the local dashboard: " + error.Message;
            HasError = true;
        }
    }

    private static void OpenUrl(string url) =>
        Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });

    private void LoadConfig()
    {
        try
        {
            if (!File.Exists(_configPath)) return;
            var config = JsonSerializer.Deserialize<WindowConfig>(File.ReadAllText(_configPath));
            if (config is null) return;
            Topmost = config.Topmost;
            if (config.Left is >= -10000 and <= 10000) Left = config.Left.Value;
            if (config.Top is >= -10000 and <= 10000) Top = config.Top.Value;
        }
        catch (Exception) { }
    }

    private void SaveConfig()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_configPath)!);
            File.WriteAllText(_configPath, JsonSerializer.Serialize(new WindowConfig(Topmost, Left, Top)));
        }
        catch (Exception) { }
    }

    private void Set<T>(ref T field, T value, [CallerMemberName] string? name = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value)) return;
        field = value;
        OnPropertyChanged(name);
    }

    private void OnPropertyChanged([CallerMemberName] string? name = null) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));

    private sealed record WindowConfig(bool Topmost, double? Left, double? Top);
}

internal static class ObservableCollectionExtensions
{
    public static void ReplaceWith<T>(this ObservableCollection<T> collection, IEnumerable<T> values)
    {
        collection.Clear();
        foreach (var value in values) collection.Add(value);
    }
}
