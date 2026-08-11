namespace BjtuHpc.Widget.Core;

public sealed record AccountDisplay(
    string Alias, int Running, int Pending, int RunningGpus, int RunningCpus,
    string Status, string StatusTone, bool NeedsLogin);

public sealed record NodeDisplay(
    string Name, string State, int GpuFree, int GpuTotal, int CpuFree, int CpuTotal);

public sealed record SnapshotPresentation(
    int GpuFree, int GpuTotal, int CpuFree, int CpuTotal, int Running, int Pending,
    int AccountCount, int AttentionCount, string UpdatedLabel, bool IsStale,
    IReadOnlyList<AccountDisplay> Accounts, IReadOnlyList<NodeDisplay> Nodes)
{
    public static SnapshotPresentation From(HpcSnapshot snapshot, DateTimeOffset now)
    {
        var accounts = snapshot.Payload?.Accounts ?? [];
        var guardian = snapshot.Guardian?.Accounts;
        var rows = accounts.Select(account =>
        {
            GuardianAccount? health = null;
            guardian?.TryGetValue(account.Name ?? string.Empty, out health);
            var needsLogin = health?.NeedsVisibleLogin == true || account.HasToken == false;
            var failed = account.Error is not null ||
                new[] { "error", "expired", "invalid", "missing" }
                    .Contains(health?.Status, StringComparer.OrdinalIgnoreCase);
            var warning = health?.AttentionRequired == true || health?.AgeWarning == true;
            var status = needsLogin ? "LOGIN" : failed ? "ERR" : warning ? "WARN" : "OK";
            var tone = needsLogin ? "Purple" : failed ? "Red" : warning ? "Orange" : "Green";
            return new AccountDisplay(
                account.Name ?? "account", account.Summary?.Running ?? 0,
                account.Summary?.Pending ?? 0, account.Summary?.RunningGpus ?? 0,
                account.Summary?.RunningCpus ?? 0, status, tone, needsLogin);
        }).OrderBy(row => row.Status switch
        {
            "LOGIN" => 0, "ERR" => 1, "WARN" => 2,
            _ when row.Pending > 0 => 3,
            _ when row.Running > 0 => 4,
            _ => 5
        }).ThenBy(row => row.Alias, StringComparer.OrdinalIgnoreCase).ToArray();

        var nodes = (snapshot.Payload?.ClusterResources?.Nodes ?? [])
            .Select(node => new NodeDisplay(
                node.Name ?? "GPU", node.State ?? "UNKNOWN",
                ResolveAvailable(node.GpuFree, node.GpuTotal, node.GpuAlloc),
                node.GpuTotal ?? 0,
                ResolveAvailable(node.CpuFree, node.CpuTotal, node.CpuAlloc),
                node.CpuTotal ?? 0))
            .Take(8).ToArray();
        var summary = snapshot.Payload?.ClusterResources?.Summary;
        var written = ParseTime(snapshot.WrittenAt) ?? ParseTime(snapshot.Payload?.CheckedAtLocal);
        var stale = written is null || now - written > TimeSpan.FromMinutes(3);
        return new SnapshotPresentation(
            ResolveAvailable(summary?.GpuFree, summary?.GpuTotal, summary?.GpuAlloc,
                nodes.Sum(n => n.GpuFree)),
            summary?.GpuTotal ?? nodes.Sum(n => n.GpuTotal),
            ResolveAvailable(summary?.CpuFree, summary?.CpuTotal, summary?.CpuAlloc,
                nodes.Sum(n => n.CpuFree)),
            summary?.CpuTotal ?? nodes.Sum(n => n.CpuTotal),
            rows.Sum(row => row.Running), rows.Sum(row => row.Pending), rows.Length,
            rows.Count(row => row.Status != "OK"),
            written?.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss") ?? "unknown",
            stale, rows, nodes);
    }

    private static DateTimeOffset? ParseTime(string? value) =>
        DateTimeOffset.TryParse(value, out var parsed) ? parsed : null;

    public static int ResolveAvailable(int? reported, int? total, int? allocated, int fallback = 0)
    {
        var derived = total.HasValue && allocated.HasValue
            ? Math.Max(0, total.Value - allocated.Value)
            : fallback;
        if (!reported.HasValue || reported.Value < 0 ||
            total.HasValue && reported.Value > total.Value ||
            reported.Value == 0 && derived > 0)
        {
            return derived;
        }
        return reported.Value;
    }
}
