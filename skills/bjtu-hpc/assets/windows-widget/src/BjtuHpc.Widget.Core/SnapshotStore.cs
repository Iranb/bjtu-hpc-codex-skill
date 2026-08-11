using System.Text.Json;

namespace BjtuHpc.Widget.Core;

public sealed class SnapshotStore
{
    public const int MaximumSnapshotBytes = 1_048_576;
    private static readonly HashSet<string> ForbiddenSecretFields = new(StringComparer.OrdinalIgnoreCase)
    {
        "token", "password", "login_password", "cookie", "cookies", "secret",
        "private_key", "certificate_token", "temporary_certificate"
    };

    public static string DefaultDirectory =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "BJTUHPCWidget");

    public static string DefaultPath => Path.Combine(DefaultDirectory, "snapshot.json");

    public static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
        WriteIndented = true
    };

    public async Task<HpcSnapshot> LoadAsync(string path, CancellationToken cancellationToken = default)
    {
        var info = new FileInfo(path);
        if (!info.Exists)
        {
            throw new FileNotFoundException("No redacted HPC snapshot has been written yet.", path);
        }
        if (info.Length is <= 0 or > MaximumSnapshotBytes)
        {
            throw new InvalidDataException("Snapshot size is outside the accepted range.");
        }

        await using var stream = new FileStream(
            path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete,
            bufferSize: 16_384, useAsync: true);
        using var document = await JsonDocument.ParseAsync(stream, cancellationToken: cancellationToken);
        RejectSecretFields(document.RootElement);
        var snapshot = document.RootElement.Deserialize<HpcSnapshot>(JsonOptions)
            ?? throw new InvalidDataException("Snapshot JSON did not contain an object.");
        Validate(snapshot);
        return snapshot;
    }

    public async Task WriteAtomicAsync(
        string path, HpcSnapshot snapshot, CancellationToken cancellationToken = default)
    {
        Validate(snapshot);
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        var tempPath = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            await using (var stream = new FileStream(
                tempPath, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                bufferSize: 16_384, useAsync: true))
            {
                await JsonSerializer.SerializeAsync(stream, snapshot, JsonOptions, cancellationToken);
                await stream.FlushAsync(cancellationToken);
            }
            File.Move(tempPath, path, overwrite: true);
        }
        finally
        {
            if (File.Exists(tempPath))
            {
                File.Delete(tempPath);
            }
        }
    }

    public static void Validate(HpcSnapshot snapshot)
    {
        if (snapshot.Version is < 0)
        {
            throw new InvalidDataException("Snapshot version cannot be negative.");
        }
        foreach (var account in snapshot.Payload?.Accounts ?? [])
        {
            if (string.IsNullOrWhiteSpace(account.Name))
            {
                throw new InvalidDataException("Every account must have a redacted alias.");
            }
            if (account.Name.Length > 64)
            {
                throw new InvalidDataException("Account aliases must be at most 64 characters.");
            }
        }
    }

    private static void RejectSecretFields(JsonElement element)
    {
        if (element.ValueKind == JsonValueKind.Object)
        {
            foreach (var property in element.EnumerateObject())
            {
                if (ForbiddenSecretFields.Contains(property.Name))
                {
                    throw new InvalidDataException($"Snapshot contains forbidden secret field '{property.Name}'.");
                }
                RejectSecretFields(property.Value);
            }
        }
        else if (element.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in element.EnumerateArray())
            {
                RejectSecretFields(item);
            }
        }
    }
}
