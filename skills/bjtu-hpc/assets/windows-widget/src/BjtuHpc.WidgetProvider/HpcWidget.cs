using System.Diagnostics;
using System.Net.Http.Json;
using System.Text.Json;
using BjtuHpc.Widget.Core;
using Microsoft.Windows.Widgets.Providers;

namespace BjtuHpc.WidgetProvider;

internal sealed class HpcWidget
{
    public const string DefinitionId = "BJTU_HPC_Status";
    private const int AccountsPerPage = 4;
    private readonly string _widgetId;
    private readonly SnapshotStore _store = new();
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(8) };
    private HashSet<string> _knownAccounts = new(StringComparer.OrdinalIgnoreCase);
    private string? _notice;
    private int _page;

    public HpcWidget(string widgetId, string initialState)
    {
        _widgetId = widgetId;
        if (int.TryParse(initialState, out var page) && page >= 0) _page = page;
    }

    public void Update()
    {
        var options = new WidgetUpdateRequestOptions(_widgetId)
        {
            Template = File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Templates", "HpcWidgetTemplate.json")),
            Data = BuildData(),
            CustomState = _page.ToString()
        };
        WidgetManager.GetDefault().UpdateWidget(options);
    }

    public void OnActionInvoked(WidgetActionInvokedArgs args)
    {
        switch (args.Verb)
        {
            case "previousPage": _page = Math.Max(0, _page - 1); break;
            case "nextPage": _page++; break;
            case "openDashboard":
                Process.Start(new ProcessStartInfo("http://127.0.0.1:8765/") { UseShellExecute = true });
                break;
            case "refreshAllTokens":
                RequestVisibleRefresh(new { accounts = "all" }, "all saved accounts");
                break;
            case "refreshAccount":
                var alias = ReadAccountAlias(args.Data);
                if (alias is not null && _knownAccounts.Contains(alias))
                {
                    RequestVisibleRefresh(new { account = alias }, alias);
                }
                else
                {
                    _notice = "Account refresh request rejected.";
                }
                break;
            case "refresh": break;
        }
        Update();
    }

    private string BuildData()
    {
        try
        {
            var snapshot = _store.LoadAsync(
                Environment.GetEnvironmentVariable("HPC_WIDGET_SNAPSHOT") ?? SnapshotStore.DefaultPath)
                .GetAwaiter().GetResult();
            var model = SnapshotPresentation.From(snapshot, DateTimeOffset.Now);
            _knownAccounts = model.Accounts.Select(account => account.Alias)
                .ToHashSet(StringComparer.OrdinalIgnoreCase);
            var pageCount = Math.Max(1, (int)Math.Ceiling(model.Accounts.Count / (double)AccountsPerPage));
            _page %= pageCount;
            var accounts = model.Accounts.Skip(_page * AccountsPerPage).Take(AccountsPerPage)
                .Select(account => new
                {
                    alias = account.Alias, status = account.Status,
                    running = account.Running, pending = account.Pending,
                    runningGpus = account.RunningGpus,
                    needsLogin = account.NeedsLogin
                }).ToArray();
            var nodes = model.Nodes.Take(6).Select(node => new
            {
                name = node.Name,
                state = node.State,
                gpuFree = node.GpuFree,
                gpuTotal = node.GpuTotal
            }).ToArray();
            return JsonSerializer.Serialize(new
            {
                generation = WidgetGeneration.DisplayVersion,
                gpuFree = model.GpuFree, gpuTotal = model.GpuTotal,
                cpuFree = model.CpuFree, cpuTotal = model.CpuTotal,
                running = model.Running, pending = model.Pending, accounts, nodes,
                page = _page + 1, pageCount, updated = model.UpdatedLabel,
                status = model.IsStale ? "STALE" : model.AttentionCount > 0 ? "ATTENTION" : "OK",
                legend = "OK healthy · WARN review · ERR failed · LOGIN sign in",
                notice = _notice ?? string.Empty,
                hasNotice = _notice is not null
            });
        }
        catch (Exception error) when (error is IOException or JsonException or UnauthorizedAccessException)
        {
            return JsonSerializer.Serialize(new
            {
                generation = WidgetGeneration.DisplayVersion,
                gpuFree = 0, gpuTotal = 0, cpuFree = 0, cpuTotal = 0,
                running = 0, pending = 0, accounts = Array.Empty<object>(), nodes = Array.Empty<object>(),
                page = 0, pageCount = 0, updated = "No trusted snapshot", status = "UNAVAILABLE",
                legend = "OK healthy · WARN review · ERR failed · LOGIN sign in",
                notice = error.Message,
                hasNotice = true
            });
        }
    }

    private void RequestVisibleRefresh(object selection, string label)
    {
        try
        {
            var response = _http.PostAsJsonAsync(
                "http://127.0.0.1:8765/api/token-guardian/visible-refresh", selection)
                .GetAwaiter().GetResult();
            response.EnsureSuccessStatusCode();
            _notice = $"Visible login requested for {label}.";
        }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException)
        {
            _notice = "Local dashboard unavailable: " + error.Message;
        }
    }

    private static string? ReadAccountAlias(string? data)
    {
        if (string.IsNullOrWhiteSpace(data)) return null;
        try
        {
            using var document = JsonDocument.Parse(data);
            return document.RootElement.TryGetProperty("account", out var account) &&
                account.ValueKind == JsonValueKind.String
                ? account.GetString()
                : null;
        }
        catch (JsonException)
        {
            return null;
        }
    }
}
