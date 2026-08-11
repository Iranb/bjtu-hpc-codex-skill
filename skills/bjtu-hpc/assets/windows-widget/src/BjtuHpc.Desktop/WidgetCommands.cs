using System.Windows.Input;

namespace BjtuHpc.Desktop;

public static class WidgetCommands
{
    public static readonly RoutedUICommand Reload = new(
        "Reload snapshot", nameof(Reload), typeof(WidgetCommands),
        [new KeyGesture(Key.R, ModifierKeys.Control)]);

    public static readonly RoutedUICommand OpenDashboard = new(
        "Open dashboard", nameof(OpenDashboard), typeof(WidgetCommands),
        [new KeyGesture(Key.D, ModifierKeys.Control)]);

    public static readonly RoutedUICommand RefreshTokens = new(
        "Refresh all tokens", nameof(RefreshTokens), typeof(WidgetCommands),
        [new KeyGesture(Key.T, ModifierKeys.Control)]);

    public static readonly RoutedUICommand TogglePin = new(
        "Toggle always on top", nameof(TogglePin), typeof(WidgetCommands));

    public static readonly RoutedUICommand Close = new(
        "Close widget", nameof(Close), typeof(WidgetCommands),
        [new KeyGesture(Key.Escape)]);
}
