package uz.radi.team;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * RADI Team — мобильное приложение: кабинет сотрудника/руководителя (тот же, что в Telegram).
 * - запрет скриншотов и записи экрана (FLAG_SECURE), без копирования и долгого нажатия;
 * - вход через бот сотрудников (ссылка t.me открывается в Telegram);
 * - кнопка «Назад» работает внутри кабинета;
 * - выбор фото (аватар, фото в постах).
 */
public class MainActivity extends Activity {
    private static final int FILE_REQ = 7;
    private WebView web;
    private ValueCallback<Uri[]> fileCallback;
    private String host;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // Запрет снимков экрана, записи экрана и превью в списке недавних приложений
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE);
        getWindow().setStatusBarColor(Color.parseColor("#06050C"));

        host = Uri.parse(BuildConfig.SERVER).getHost();
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#06050C"));
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setUserAgentString(s.getUserAgentString() + " RadiTeamApp/" + BuildConfig.VERSION_NAME);
        CookieManager.getInstance().setAcceptCookie(true);

        // Без долгого нажатия: не появляется меню «Копировать / Выделить всё»
        web.setLongClickable(false);
        web.setHapticFeedbackEnabled(false);
        web.setOnLongClickListener(v -> true);

        web.addJavascriptInterface(new Bridge(), "RadiApp");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                if (host != null && host.equals(u.getHost())) return false;  // свой сервер — внутри приложения
                openExternal(u.toString());  // Telegram, телефоны, другие сайты — во внешнем приложении
                return true;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) showOffline();
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = callback;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("image/*");
                try {
                    startActivityForResult(Intent.createChooser(i, "Rasm / Фото"), FILE_REQ);
                } catch (ActivityNotFoundException e) {
                    fileCallback = null;
                    return false;
                }
                return true;
            }
        });

        if (savedInstanceState != null) web.restoreState(savedInstanceState);
        else web.loadUrl(BuildConfig.SERVER + "?app=android");
    }

    private void showOffline() {
        String html = "<html><body style='background:#06050C;color:#f5f3ff;font-family:sans-serif;text-align:center;padding-top:40%'>"
                + "<div style='font-size:40px'>📡</div><h2>Internet yo'q / Нет интернета</h2>"
                + "<button style='margin-top:20px;padding:14px 28px;border:0;border-radius:14px;background:#8b5cf6;color:#fff;font-size:16px' "
                + "onclick=\"location.href='" + BuildConfig.SERVER + "?app=android'\">Qayta urinish / Повторить</button></body></html>";
        web.loadDataWithBaseURL(BuildConfig.SERVER, html, "text/html", "utf-8", null);
    }

    private void openExternal(String url) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        } catch (ActivityNotFoundException e) {
            if (url.startsWith("tg:")) return;
            try { startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url))); } catch (Exception ignored) { }
        }
    }

    /** Мост для страницы: window.RadiApp.openExternal(url) */
    private class Bridge {
        @JavascriptInterface
        public void openExternal(final String url) {
            runOnUiThread(() -> MainActivity.this.openExternal(url));
        }

        @JavascriptInterface
        public String version() { return BuildConfig.VERSION_NAME; }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_REQ && fileCallback != null) {
            Uri[] res = null;
            if (resultCode == RESULT_OK && data != null && data.getData() != null) res = new Uri[]{data.getData()};
            fileCallback.onReceiveValue(res);
            fileCallback = null;
        }
    }

    @Override
    public void onBackPressed() {
        // сначала — «назад» внутри кабинета (закрыть окно, вернуться на прошлый экран); если некуда — свернуть
        web.evaluateJavascript("(window.appBack && window.appBack()) ? 'yes' : 'no'", value -> {
            if (!"\"yes\"".equals(value)) moveTaskToBack(true);
        });
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
        // вернулись из Telegram после входа — страница сама ждёт подтверждения
    }

    @Override
    protected void onPause() {
        web.onPause();
        super.onPause();
    }
}
