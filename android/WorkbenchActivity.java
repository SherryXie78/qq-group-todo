package com.codex.todoapp;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.net.VpnService;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputType;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebChromeClient;
import android.webkit.ValueCallback;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

/** A native task home sharing the embedded, upstream Tailscale service. */
public final class WorkbenchActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private WebView web;
    private TextView status;
    private SharedPreferences preferences;
    private String address;
    private String host;
    private boolean resumed;
    private boolean loadingFailed;
    private boolean askingConsent;
    private boolean consentRejected;
    private long lastConnect;
    private ValueCallback<Uri[]> photoCallback;
    private static final int PICK_AVATAR = 7002;
    private final Runnable retry = new Runnable() {
        @Override public void run() {
            if (!resumed || web == null || address == null || !loadingFailed) return;
            connect(false);
            web.loadUrl(address);
        }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        preferences = getSharedPreferences("workbench_private", MODE_PRIVATE);
        address = preferences.getString("address", null);
        if (address != null && !valid(address)) address = null;
        if (address != null) host = Uri.parse(address).getHost();
        getWindow().setStatusBarColor(Color.rgb(242, 246, 252));
        getWindow().setNavigationBarColor(Color.rgb(242, 246, 252));
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR);
        showWorkbench();
        if (address == null) handler.post(new Runnable() {
            @Override public void run() { configure(); }
        });
    }

    private int dp(int value) {
        return (int) (getResources().getDisplayMetrics().density * value + .5f);
    }

    private Button button(String title, View.OnClickListener action) {
        Button result = new Button(this);
        result.setText(title);
        result.setAllCaps(false);
        result.setTextSize(13);
        result.setOnClickListener(action);
        return result;
    }

    private void showWorkbench() {
        LinearLayout root = new LinearLayout(this);
        root.setFitsSystemWindows(true);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(242, 246, 252));
        LinearLayout toolbar = new LinearLayout(this);
        toolbar.setPadding(dp(10), 0, dp(6), 0);
        toolbar.setGravity(android.view.Gravity.CENTER_VERTICAL);
        TextView title = new TextView(this);
        title.setText("群待办"); title.setTextSize(18);
        title.setTextColor(Color.rgb(23, 33, 59));
        toolbar.addView(title, new LinearLayout.LayoutParams(0, dp(48), 1));
        title.setGravity(android.view.Gravity.CENTER_VERTICAL);
        toolbar.addView(button("刷新", new View.OnClickListener() {
            @Override public void onClick(View view) {
                if (address == null) configure();
                else { connect(true); web.loadUrl(address); }
            }
        }));
        toolbar.addView(button("设置", new View.OnClickListener() {
            @Override public void onClick(View view) { menu(); }
        }));
        root.addView(toolbar);
        status = new TextView(this);
        status.setPadding(dp(16), dp(8), dp(16), dp(8));
        status.setTextColor(Color.rgb(126, 61, 0));
        status.setText("正在建立私人连接…");
        root.addView(status);
        web = new WebView(this);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setUserAgentString(settings.getUserAgentString()+" GroupTodoNative/2026.11.01.1");
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);
        WebView.setWebContentsDebuggingEnabled(false);
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (photoCallback != null) photoCallback.onReceiveValue(null);
                photoCallback = null;
                Uri current = view.getUrl() == null ? null : Uri.parse(view.getUrl());
                if (current == null || !"https".equals(current.getScheme()) || host == null || !host.equals(current.getHost())) {
                    callback.onReceiveValue(null); return true;
                }
                photoCallback = callback;
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("image/*");
                intent.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"image/jpeg", "image/png", "image/webp"});
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                try { startActivityForResult(intent, PICK_AVATAR); }
                catch (RuntimeException error) {
                    photoCallback.onReceiveValue(null); photoCallback = null;
                    Toast.makeText(WorkbenchActivity.this, "暂时无法打开图片选择器，请在电脑上传头像。", Toast.LENGTH_LONG).show();
                }
                return true;
            }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return navigate(request.getUrl());
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return navigate(Uri.parse(url));
            }
            @Override public void onPageStarted(WebView view, String url, android.graphics.Bitmap icon) {
                loadingFailed = false;
            }
            @Override public void onPageFinished(WebView view, String url) {
                if (!loadingFailed && host != null && host.equals(Uri.parse(url).getHost())) {
                    status.setVisibility(View.GONE);
                    CookieManager.getInstance().flush();
                }
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) disconnected();
            }
            @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (request.isForMainFrame() && response.getStatusCode() == 401) {
                    loadingFailed = true;
                    status.setText("工作台授权已失效，请在设置中重新粘贴电脑提供的私人访问地址。");
                    status.setVisibility(View.VISIBLE);
                }
            }
        });
        web.setDownloadListener(new DownloadListener() {
            @Override public void onDownloadStart(String url, String userAgent, String disposition, String mime, long size) {
                Uri uri = Uri.parse(url);
                if (!"https".equals(uri.getScheme()) || host == null || !host.equals(uri.getHost())) return;
                try {
                    DownloadManager.Request request = new DownloadManager.Request(uri);
                    String cookie = CookieManager.getInstance().getCookie(url);
                    if (cookie != null) request.addRequestHeader("Cookie", cookie);
                    String filename = "text/calendar".equals(mime) ? "群待办日程.ics" :
                        "application/vnd.android.package-archive".equals(mime) ? "群待办更新-2026.11.01.1.apk" : "群待办下载";
                    request.setTitle(filename);
                    request.setMimeType(mime);
                    request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    if (Build.VERSION.SDK_INT >= 29) request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename);
                    else request.setDestinationInExternalFilesDir(WorkbenchActivity.this, Environment.DIRECTORY_DOWNLOADS, filename);
                    ((DownloadManager)getSystemService(DOWNLOAD_SERVICE)).enqueue(request);
                    Toast.makeText(WorkbenchActivity.this, "已开始下载，可从系统下载通知打开。", Toast.LENGTH_LONG).show();
                } catch (RuntimeException error) {
                    Toast.makeText(WorkbenchActivity.this, "下载未完成，请检查私人连接后重试。", Toast.LENGTH_LONG).show();
                }
            }
        });
        root.addView(web, new LinearLayout.LayoutParams(-1, 0, 1));
        setContentView(root);
        if (address != null) web.loadUrl(address);
        else status.setText("首次使用：登录内置连接，然后添加电脑的私人访问地址。");
    }

    private boolean navigate(Uri uri) {
        if ("https".equals(uri.getScheme()) && host != null && host.equals(uri.getHost()) && uri.getUserInfo() == null) return false;
        // No JS bridge, file URLs, arbitrary intents, or insecure TLS exceptions.
        Toast.makeText(this, "此链接请在电脑或手机浏览器中打开。", Toast.LENGTH_LONG).show();
        return true;
    }

    private void disconnected() {
        loadingFailed = true;
        status.setText("暂时连不上电脑，正在自动重试。请确认电脑开机、助手运行；首次使用需先登录内置连接。");
        status.setVisibility(View.VISIBLE);
        handler.removeCallbacks(retry);
        handler.postDelayed(retry, 12000);
    }

    private void connect(boolean force) {
        if (address == null) return;
        long now = android.os.SystemClock.elapsedRealtime();
        if (askingConsent || (!force && consentRejected)) return;
        if (!force && lastConnect != 0 && now-lastConnect < 30000) return;
        Intent consent = VpnService.prepare(this);
        if (consent != null) {
            if (resumed) { askingConsent = true; startActivityForResult(consent, 7001); }
            return;
        }
        android.net.ConnectivityManager connectivity = (android.net.ConnectivityManager)getSystemService(CONNECTIVITY_SERVICE);
        android.net.NetworkCapabilities capabilities = connectivity.getNetworkCapabilities(connectivity.getActiveNetwork());
        if (capabilities != null && capabilities.hasTransport(android.net.NetworkCapabilities.TRANSPORT_VPN)) return;
        lastConnect = now;
        Intent intent = new Intent("com.tailscale.ipn.CONNECT_VPN");
        intent.setComponent(new ComponentName(getPackageName(), "com.tailscale.ipn.IPNReceiver"));
        sendBroadcast(intent);
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == PICK_AVATAR) {
            ValueCallback<Uri[]> callback = photoCallback; photoCallback = null;
            if (callback != null) {
                Uri selected = result == RESULT_OK && data != null ? data.getData() : null;
                Uri current = web == null || web.getUrl() == null ? null : Uri.parse(web.getUrl());
                String mime = null;
                try { if (selected != null) mime = getContentResolver().getType(selected); } catch (RuntimeException ignored) {}
                boolean trustedPage = current != null && "https".equals(current.getScheme()) && host != null && host.equals(current.getHost());
                boolean image = "image/jpeg".equals(mime) || "image/png".equals(mime) || "image/webp".equals(mime);
                callback.onReceiveValue(trustedPage && selected != null && "content".equals(selected.getScheme()) && image ? new Uri[]{selected} : null);
            }
            return;
        }
        if (request == 7001) { askingConsent = false; consentRejected = result != RESULT_OK; }
        if (request == 7001 && result == RESULT_OK) {
            connect(true);
            if (address != null) web.loadUrl(address);
        }
    }

    private boolean valid(String value) {
        try {
            Uri uri = Uri.parse(value);
            return "https".equals(uri.getScheme()) && uri.getHost() != null
                && uri.getHost().matches("[a-z0-9][a-z0-9.-]*\\.ts\\.net")
                && uri.getUserInfo() == null && (uri.getPort() == -1 || uri.getPort() == 443)
                && (uri.getPath() == null || "/".equals(uri.getPath()) || uri.getPath().isEmpty());
        } catch (RuntimeException error) { return false; }
    }

    private void configure() {
        final EditText field = new EditText(this);
        field.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        field.setSingleLine(false); field.setMinLines(3);
        field.setText(address == null ? "" : address);
        field.setHint("粘贴电脑工作台提供的 https://…ts.net/?token=… 地址");
        LinearLayout body = new LinearLayout(this); body.setOrientation(LinearLayout.VERTICAL);
        body.setPadding(dp(22), dp(8), dp(22), 0);
        TextView hint = new TextView(this);
        hint.setText("只需配置一次。请从自己的工作台复制私人访问地址；不要填写QQ密码或DeepSeek Key。地址仅保存在本应用内部。");
        body.addView(hint); body.addView(field);
        body.addView(button("从剪贴板粘贴", new View.OnClickListener() {
            @Override public void onClick(View view) {
                ClipboardManager clipboard = (ClipboardManager)getSystemService(CLIPBOARD_SERVICE);
                if (clipboard.hasPrimaryClip() && clipboard.getPrimaryClip().getItemCount()>0)
                    field.setText(clipboard.getPrimaryClip().getItemAt(0).coerceToText(WorkbenchActivity.this));
            }
        }));
        final AlertDialog dialog = new AlertDialog.Builder(this).setTitle("连接我的工作台").setView(body)
            .setPositiveButton("保存并连接", null).setNeutralButton("内置连接登录", null).setNegativeButton("稍后", null).create();
        dialog.setOnShowListener(new android.content.DialogInterface.OnShowListener() {
            @Override public void onShow(android.content.DialogInterface ignored) {
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(new View.OnClickListener() {
                    @Override public void onClick(View view) {
                        String value = field.getText().toString().trim();
                        if (!valid(value)) { field.setError("请粘贴完整的私人HTTPS工作台地址。"); return; }
                        address = value; host = Uri.parse(value).getHost();
                        preferences.edit().putString("address", value).apply();
                        dialog.dismiss(); connect(true); web.loadUrl(address);
                    }
                });
                dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener(new View.OnClickListener() {
                    @Override public void onClick(View view) { openConnection(); }
                });
            }
        });
        dialog.show();
    }

    private void openConnection() {
        startActivity(new Intent().setComponent(new ComponentName(getPackageName(), "com.tailscale.ipn.MainActivity")));
    }

    private void menu() {
        new AlertDialog.Builder(this).setTitle("群待办设置").setItems(new String[]{"内置连接：登录 / 状态", "修改工作台访问地址", "后台常驻设置", "关于整合版"},
            new android.content.DialogInterface.OnClickListener() {
                @Override public void onClick(android.content.DialogInterface dialog, int item) {
                    if (item == 0) openConnection();
                    if (item == 1) configure();
                    if (item == 2) startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:"+getPackageName())));
                    if (item == 3) new AlertDialog.Builder(WorkbenchActivity.this).setTitle("群待办 · 内置私人连接")
                        .setMessage("本地个人整合版，基于Tailscale Android 1.104.2，另行签名，由Codex为本机部署。非Tailscale官方产品。\n\n连接核心沿用上游客户端，保留上游开源许可。打开本应用会尝试连接自己的私人网络。电脑仍需开机，QQ与助手仍需运行。\n\nDeepSeek Key和QQ群采集都在电脑上；安装包不包含密码、API Key或工作台授权。")
                        .setPositiveButton("知道了", null).show();
                }
            }).show();
    }

    @Override protected void onResume() {
        super.onResume(); resumed = true;
        if (web != null) web.onResume();
        if (address != null) {
            connect(false);
            if (loadingFailed) web.loadUrl(address);
            else web.evaluateJavascript("window.dispatchEvent(new Event('focus'));", null);
        }
    }

    @Override protected void onPause() {
        resumed = false; handler.removeCallbacks(retry);
        if (web != null) web.onPause();
        CookieManager.getInstance().flush(); super.onPause();
    }

    @Override public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else moveTaskToBack(true);
    }

    @Override protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (photoCallback != null) { photoCallback.onReceiveValue(null); photoCallback = null; }
        if (web != null) web.destroy(); super.onDestroy();
    }
}
