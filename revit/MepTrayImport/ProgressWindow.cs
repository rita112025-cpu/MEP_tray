using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Threading;

namespace MepTray.Revit;

/// <summary>
/// 非模態進度視窗（WPF，程式碼建立）。在命令的 API 主執行緒中逐項更新並抽取訊息佇列，讓「取消」按鈕可回應；
/// 不使用背景執行緒，Revit API 仍只在命令內呼叫。
/// </summary>
internal sealed class ProgressWindow : Window
{
    readonly TextBlock _text = new() { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 8) };
    readonly ProgressBar _bar = new() { Height = 18, Minimum = 0, Maximum = 1 };
    public bool CancelRequested { get; private set; }

    public ProgressWindow(string title, IntPtr owner)
    {
        Title = title; Width = 520; SizeToContent = SizeToContent.Height; ResizeMode = ResizeMode.NoResize;
        WindowStartupLocation = WindowStartupLocation.CenterScreen; ShowInTaskbar = false; Topmost = true;
        var cancel = new Button { Content = "取消（處理完目前檔案後停止）", Margin = new Thickness(0, 10, 0, 0), Padding = new Thickness(8, 3, 8, 3),
                                  HorizontalAlignment = HorizontalAlignment.Right };
        cancel.Click += (_, _) => { CancelRequested = true; cancel.IsEnabled = false; _text.Text += "\n正在取消…"; };
        Closing += (_, e) => { if (!_done) { CancelRequested = true; e.Cancel = true; } };
        var panel = new StackPanel { Margin = new Thickness(12) };
        panel.Children.Add(_text); panel.Children.Add(_bar); panel.Children.Add(cancel);
        Content = panel;
        if (owner != IntPtr.Zero) new WindowInteropHelper(this).Owner = owner;
    }

    bool _done;

    public void Report(string phase, int done, int total, string item)
    {
        _bar.Maximum = Math.Max(1, total); _bar.Value = Math.Min(done, Math.Max(1, total));
        _text.Text = $"{phase}：{done}/{total}\n{item}" + (CancelRequested ? "\n正在取消…" : "");
        Pump();
    }

    /// <summary>抽取 WPF 訊息（同執行緒），讓畫面更新與取消按鈕生效。</summary>
    public void Pump() => Dispatcher.Invoke(DispatcherPriority.Background, new Action(() => { }));

    public void Finish() { _done = true; Close(); }
}