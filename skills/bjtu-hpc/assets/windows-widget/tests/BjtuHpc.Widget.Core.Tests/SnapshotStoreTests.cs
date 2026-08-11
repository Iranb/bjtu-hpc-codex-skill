using System.Text.Json;
using BjtuHpc.Widget.Core;

namespace BjtuHpc.Widget.Core.Tests;

public sealed class SnapshotStoreTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), "bjtu-hpc-widget-tests-" + Guid.NewGuid().ToString("N"));

    [Fact]
    public async Task AtomicRoundTripPreservesRedactedSnapshot()
    {
        var path = Path.Combine(_directory, "snapshot.json");
        var store = new SnapshotStore();
        var original = DemoSnapshot.Create();

        await store.WriteAtomicAsync(path, original);
        var loaded = await store.LoadAsync(path);

        Assert.Equal(6, loaded.Payload?.Accounts?.Count);
        Assert.Equal(12, loaded.Payload?.ClusterResources?.Summary?.GpuFree);
        Assert.Empty(Directory.GetFiles(_directory, "*.tmp"));
    }

    [Fact]
    public async Task SecretBearingSnapshotIsRejected()
    {
        Directory.CreateDirectory(_directory);
        var path = Path.Combine(_directory, "snapshot.json");
        await File.WriteAllTextAsync(path, "{\"version\":1,\"token\":\"must-not-load\"}");

        var error = await Assert.ThrowsAsync<InvalidDataException>(() => new SnapshotStore().LoadAsync(path));

        Assert.Contains("forbidden secret field", error.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void PresentationPrioritizesLoginAndComputesTotals()
    {
        var now = DateTimeOffset.Parse("2026-08-11T14:00:00+08:00");
        var presentation = SnapshotPresentation.From(DemoSnapshot.Create(now), now);

        Assert.Equal(12, presentation.GpuFree);
        Assert.Equal(3, presentation.Running);
        Assert.Equal(1, presentation.Pending);
        Assert.Equal("acct-c", presentation.Accounts[0].Alias);
        Assert.Equal("LOGIN", presentation.Accounts[0].Status);
        Assert.False(presentation.IsStale);
    }

    [Fact]
    public void PresentationFallsBackFromContradictoryLegacyFreeCounts()
    {
        var now = DateTimeOffset.Parse("2026-08-11T14:00:00+08:00");
        var original = DemoSnapshot.Create(now);
        var resources = original.Payload!.ClusterResources!;
        var summary = resources.Summary! with
        {
            GpuTotal = 16,
            GpuAlloc = 4,
            GpuFree = 0,
            CpuTotal = 96,
            CpuAlloc = 24,
            CpuFree = 0
        };
        var firstNode = resources.Nodes![0] with
        {
            GpuTotal = 8,
            GpuAlloc = 3,
            GpuFree = 0
        };
        var snapshot = original with
        {
            Payload = original.Payload with
            {
                ClusterResources = resources with
                {
                    Summary = summary,
                    Nodes = new[] { firstNode }.Concat(resources.Nodes.Skip(1)).ToArray()
                }
            }
        };

        var presentation = SnapshotPresentation.From(snapshot, now);

        Assert.Equal(12, presentation.GpuFree);
        Assert.Equal(72, presentation.CpuFree);
        Assert.Equal(5, presentation.Nodes[0].GpuFree);
    }

    [Fact]
    public void PresentationKeepsValidReportedAvailability()
    {
        Assert.Equal(5, SnapshotPresentation.ResolveAvailable(5, 16, 4));
    }

    [Fact]
    public async Task SnakeCaseContractLoadsAppleCompatibleFields()
    {
        Directory.CreateDirectory(_directory);
        var path = Path.Combine(_directory, "snapshot.json");
        var json = JsonSerializer.Serialize(new
        {
            version = 1,
            written_at = DateTimeOffset.Now.ToString("O"),
            payload = new
            {
                accounts = new[] { new { name = "acct-safe", has_token = true } },
                cluster_resources = new { summary = new { gpu_free = 2, gpu_total = 8 } }
            }
        });
        await File.WriteAllTextAsync(path, json);

        var loaded = await new SnapshotStore().LoadAsync(path);

        Assert.True(loaded.Payload?.Accounts?[0].HasToken);
        Assert.Equal(2, loaded.Payload?.ClusterResources?.Summary?.GpuFree);
    }

    public void Dispose()
    {
        if (Directory.Exists(_directory))
        {
            Directory.Delete(_directory, recursive: true);
        }
    }
}
