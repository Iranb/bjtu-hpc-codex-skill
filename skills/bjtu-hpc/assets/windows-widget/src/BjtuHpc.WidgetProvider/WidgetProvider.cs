using System.Runtime.InteropServices;
using Microsoft.Windows.Widgets.Providers;

namespace BjtuHpc.WidgetProvider;

[ComVisible(true)]
[ComDefaultInterface(typeof(IWidgetProvider))]
[Guid("A2F37C2E-7D64-4D09-9C5A-8F2B8191C642")]
public sealed class WidgetProvider : IWidgetProvider
{
    private static readonly Dictionary<string, HpcWidget> Instances = new();
    private static bool _recovered;

    public WidgetProvider() => Recover();

    public void CreateWidget(WidgetContext context)
    {
        if (context.DefinitionId != HpcWidget.DefinitionId)
            throw new InvalidOperationException("Unknown widget definition.");
        var widget = new HpcWidget(context.Id, string.Empty);
        Instances[context.Id] = widget;
        widget.Update();
    }

    public void DeleteWidget(string widgetId, string customState) => Instances.Remove(widgetId);

    public void OnActionInvoked(WidgetActionInvokedArgs args)
    {
        if (Instances.TryGetValue(args.WidgetContext.Id, out var widget)) widget.OnActionInvoked(args);
    }

    public void OnWidgetContextChanged(WidgetContextChangedArgs args)
    {
        if (Instances.TryGetValue(args.WidgetContext.Id, out var widget)) widget.Update();
    }

    public void Activate(WidgetContext context)
    {
        if (Instances.TryGetValue(context.Id, out var widget)) widget.Update();
    }

    public void Deactivate(string widgetId) { }

    private static void Recover()
    {
        if (_recovered) return;
        try
        {
            foreach (var info in WidgetManager.GetDefault().GetWidgetInfos())
            {
                var context = info.WidgetContext;
                if (context.DefinitionId == HpcWidget.DefinitionId)
                    Instances[context.Id] = new HpcWidget(context.Id, info.CustomState);
            }
        }
        finally { _recovered = true; }
    }
}
