namespace BjtuHpc.Widget.Core;

public static class DemoSnapshot
{
    public static HpcSnapshot Create(DateTimeOffset? now = null)
    {
        var timestamp = (now ?? DateTimeOffset.Now).ToString("O");
        return new HpcSnapshot
        {
            Version = 1,
            WrittenAt = timestamp,
            Payload = new HpcPayload
            {
                CheckedAtLocal = timestamp,
                Accounts =
                [
                    Account("acct-a", true, 1, 0, 1, 6), Account("acct-b", true, 1, 1, 1, 6),
                    Account("acct-c", false, 0, 0, 0, 0), Account("acct-d", true, 0, 0, 0, 0),
                    Account("acct-e", true, 1, 0, 1, 6), Account("acct-f", true, 0, 0, 0, 0)
                ],
                ClusterResources = new ClusterResources
                {
                    Summary = new ClusterSummary
                    {
                        Nodes = 4, GpuAlloc = 20, GpuTotal = 32, GpuFree = 12,
                        CpuAlloc = 84, CpuTotal = 192, CpuFree = 108, ReservedNodes = 1
                    },
                    Nodes =
                    [
                        Node("gpu01", "MIXED", 0, 8, 32, 48), Node("gpu02", "MIXED", 0, 8, 5, 48),
                        Node("gpu03", "IDLE", 8, 8, 48, 48), Node("gpu04", "MIXED", 4, 8, 23, 48)
                    ],
                    ExcludedReservedNodes = ["gpu05"]
                }
            },
            Guardian = new GuardianPayload
            {
                Accounts = new Dictionary<string, GuardianAccount>
                {
                    ["acct-c"] = new()
                    {
                        Status = "expired", AttentionRequired = true,
                        AttentionReason = "Authentication required", NeedsVisibleLogin = true
                    }
                }
            },
            Returncode = 0
        };
    }

    private static HpcAccount Account(string name, bool hasToken, int running, int pending, int gpus, int cpus) =>
        new()
        {
            Name = name, HasToken = hasToken,
            Summary = new AccountSummary
            {
                Running = running, Pending = pending, Total = running + pending,
                RunSlotsOpen = Math.Max(0, 2 - running - pending),
                RunningGpus = gpus, RunningCpus = cpus,
                PendingReasons = pending > 0 ? new Dictionary<string, int> { ["Resources"] = pending } : null
            },
            Jobs = []
        };

    private static ClusterNode Node(string name, string state, int gpuFree, int gpuTotal, int cpuFree, int cpuTotal) =>
        new()
        {
            Name = name, State = state, GpuFree = gpuFree, GpuTotal = gpuTotal,
            GpuAlloc = gpuTotal - gpuFree, CpuFree = cpuFree, CpuTotal = cpuTotal,
            CpuAlloc = cpuTotal - cpuFree
        };
}
