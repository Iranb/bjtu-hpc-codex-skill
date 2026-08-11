using Microsoft.Windows.Widgets.Providers;

namespace BjtuHpc.WidgetProvider;

internal sealed class RegistrationManager<T> : IDisposable where T : IWidgetProvider, new()
{
    private readonly IDisposable _registration;
    private readonly ManualResetEvent _disposedEvent = new(false);
    private bool _disposed;

    private RegistrationManager(IDisposable registration) => _registration = registration;

    public static RegistrationManager<T> RegisterProvider() =>
        new(ComClassObject.Register(typeof(T).GUID, new WidgetProviderFactory<T>()));

    public ManualResetEvent GetDisposedEvent() => _disposedEvent;

    public void Dispose()
    {
        if (_disposed) return;
        _registration.Dispose();
        _disposed = true;
        _disposedEvent.Set();
        GC.SuppressFinalize(this);
    }
}
