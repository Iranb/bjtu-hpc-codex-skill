namespace BjtuHpc.Widget.Core;

public sealed record HpcSnapshot
{
    public int? Version { get; init; }
    public string? WrittenAt { get; init; }
    public HpcPayload? Payload { get; init; }
    public GuardianPayload? Guardian { get; init; }
    public string? GuardianError { get; init; }
    public string? Error { get; init; }
    public int? Returncode { get; init; }
}

public sealed record HpcPayload
{
    public string? CheckedAtLocal { get; init; }
    public IReadOnlyList<HpcAccount>? Accounts { get; init; }
    public ClusterResources? ClusterResources { get; init; }
}

public sealed record HpcAccount
{
    public string? Name { get; init; }
    public string? Error { get; init; }
    public bool? HasToken { get; init; }
    public AccountSummary? Summary { get; init; }
    public IReadOnlyList<JobPayload>? Jobs { get; init; }
}

public sealed record AccountSummary
{
    public int? Running { get; init; }
    public int? Pending { get; init; }
    public int? Other { get; init; }
    public int? Total { get; init; }
    public int? RunSlotsOpen { get; init; }
    public int? CapOpen { get; init; }
    public int? RunningCpus { get; init; }
    public int? RunningGpus { get; init; }
    public IReadOnlyDictionary<string, int>? PendingReasons { get; init; }
}

public sealed record JobPayload
{
    public string? JobId { get; init; }
    public string? State { get; init; }
    public string? Reason { get; init; }
    public string? Name { get; init; }
}

public sealed record ClusterResources
{
    public string? Error { get; init; }
    public ClusterSummary? Summary { get; init; }
    public IReadOnlyList<ClusterNode>? Nodes { get; init; }
    public IReadOnlyList<string>? ExcludedReservedNodes { get; init; }
}

public sealed record ClusterSummary
{
    public int? Nodes { get; init; }
    public int? GpuAlloc { get; init; }
    public int? GpuTotal { get; init; }
    public int? GpuFree { get; init; }
    public int? CpuAlloc { get; init; }
    public int? CpuTotal { get; init; }
    public int? CpuFree { get; init; }
    public int? ReservedNodes { get; init; }
}

public sealed record ClusterNode
{
    public string? Name { get; init; }
    public string? State { get; init; }
    public int? CpuAlloc { get; init; }
    public int? CpuTotal { get; init; }
    public int? CpuFree { get; init; }
    public int? GpuAlloc { get; init; }
    public int? GpuTotal { get; init; }
    public int? GpuFree { get; init; }
}

public sealed record GuardianPayload
{
    public IReadOnlyDictionary<string, GuardianAccount>? Accounts { get; init; }
    public string? Error { get; init; }
}

public sealed record GuardianAccount
{
    public string? Status { get; init; }
    public bool? AttentionRequired { get; init; }
    public string? AttentionReason { get; init; }
    public bool? AgeWarning { get; init; }
    public bool? NeedsVisibleLogin { get; init; }
}
