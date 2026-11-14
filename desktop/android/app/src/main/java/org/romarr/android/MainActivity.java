package org.romarr.android;

import android.app.*;
import android.os.*;
import android.content.*;
import android.net.Uri;
import android.provider.OpenableColumns;
import android.database.Cursor;
import android.webkit.*;
import android.widget.*;
import androidx.core.content.FileProvider;
import com.chaquo.python.Python;
import java.io.*;
import java.net.URI;
import org.json.JSONArray;

/** Native host for the private bundled server and remote HTTPS profiles. */
public final class MainActivity extends androidx.activity.ComponentActivity {
 private WebView web;
 private TextView status;
 private String origin = "", local = "";
 private boolean destroyed;
 private boolean wantLocal=true;
 private int navigationGeneration;
 private final java.util.concurrent.ExecutorService worker = java.util.concurrent.Executors.newSingleThreadExecutor();
 @Override public void onCreate(Bundle saved) {
  super.onCreate(saved);
  LinearLayout layout = new LinearLayout(this); layout.setOrientation(LinearLayout.VERTICAL);
  LinearLayout bar = new LinearLayout(this);
  button(bar,"This device",this::local); button(bar,"Servers",this::profiles);
  button(bar,"Import",this::importFile); button(bar,"Play file",this::chooseFile);
  layout.setOnApplyWindowInsetsListener((v,insets) -> { v.setPadding(insets.getSystemWindowInsetLeft(),insets.getSystemWindowInsetTop(),insets.getSystemWindowInsetRight(),insets.getSystemWindowInsetBottom()); return insets; });
  layout.addView(bar);
  status = new TextView(this); status.setText("Starting private ROMarr server..."); layout.addView(status);
  web = new WebView(this);
  if (BuildConfig.DEBUG) WebView.setWebContentsDebuggingEnabled(true);
  web.getSettings().setJavaScriptEnabled(true); web.getSettings().setDomStorageEnabled(true);
  web.getSettings().setAllowFileAccess(false); web.getSettings().setAllowContentAccess(false);
  web.getSettings().setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
  web.setWebViewClient(new WebViewClient() {
   @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
    Uri u=r.getUrl();
    if (sameOrigin(u.toString(),origin)) return false;
    if (r.isForMainFrame() && ("https".equals(u.getScheme()) || "http".equals(u.getScheme()))) {
     try { startActivity(new Intent(Intent.ACTION_VIEW,u)); }
     catch (ActivityNotFoundException e) { message("No browser installed."); }
    }
    return true;
   }
   @Override public void onReceivedSslError(WebView v, android.webkit.SslErrorHandler h, android.net.http.SslError e) {
    h.cancel(); message("TLS certificate rejected. Use a trusted server certificate.");
   }
  });
  layout.addView(web,new LinearLayout.LayoutParams(-1,0,1)); setContentView(layout);
  getOnBackPressedDispatcher().addCallback(this, new androidx.activity.OnBackPressedCallback(true) {
   @Override public void handleOnBackPressed() { if (web.canGoBack()) web.goBack(); else finish(); }
  });
  worker.execute(() -> {
   try {
    String url=Python.getInstance().getModule("android_backend").callAttr("start",getFilesDir().getAbsolutePath()).toString();
    runOnUiThread(() -> { if (!destroyed) { local=url; if (wantLocal) local(); } });
   } catch (Exception e) {
    runOnUiThread(() -> { if (!destroyed) status.setText("Local server failed. Remote profiles remain available."); });
    android.util.Log.e("ROMarr","Local startup failed",e);
   }
  });
 }
 private void button(LinearLayout bar,String text,Runnable action) {
  Button b=new Button(this); b.setText(text); b.setOnClickListener(v -> action.run());
  bar.addView(b,new LinearLayout.LayoutParams(0,-2,1));
 }
 private static boolean sameOrigin(String a,String b) {
  try { URI x=new URI(a),y=new URI(b); return x.getScheme().equals(y.getScheme()) && x.getHost().equals(y.getHost()) && x.getPort()==y.getPort(); }
  catch (Exception e) { return false; }
 }
 private void open(String url) { origin=url; web.loadUrl(url); status.setText(url.equals(local) ? "This device: active while app process lives" : "Remote server"); }
 private void local() {
  wantLocal=true;
  final int generation=++navigationGeneration;
  if (local.isEmpty()) return;
  worker.execute(() -> {
   try {
    String cookie=Python.getInstance().getModule("android_backend").callAttr("session_cookie").toString();
    runOnUiThread(() -> {
     if (!destroyed && wantLocal && generation==navigationGeneration) CookieManager.getInstance().setCookie(local,cookie,accepted -> {
      if (!destroyed && accepted && wantLocal && generation==navigationGeneration) open(local);
     });
    });
   } catch (Exception e) { runOnUiThread(() -> message("Could not authenticate the local session.")); }
  });
 }
 private void remote(String url) { wantLocal=false; navigationGeneration++; open(url); }
 private void profiles() {
  android.content.SharedPreferences prefs=getSharedPreferences("servers",MODE_PRIVATE);
  java.util.ArrayList<String> names=new java.util.ArrayList<>(prefs.getAll().keySet());
  java.util.Collections.sort(names); names.add("Add HTTPS server");
  new AlertDialog.Builder(this).setTitle("Server profiles").setItems(names.toArray(new String[0]),(d,n) -> {
   if (n==names.size()-1) { addProfile(); return; }
   String name=names.get(n);
   new AlertDialog.Builder(this).setTitle(name).setItems(new String[]{"Connect","Delete"},(x,a) -> {
    if (a==0) remote(prefs.getString(name,"")); else prefs.edit().remove(name).apply();
   }).show();
  }).show();
 }
 private void addProfile() {
  LinearLayout form=new LinearLayout(this); form.setOrientation(LinearLayout.VERTICAL);
  EditText name=new EditText(this), address=new EditText(this);
  name.setHint("Profile name"); address.setHint("https://romarr.example.com");
  address.setInputType(17); form.addView(name); form.addView(address);
  new AlertDialog.Builder(this).setTitle("Add server").setView(form).setNegativeButton("Cancel",null)
   .setPositiveButton("Save",(d,w) -> {
    try {
     URI u=new URI(address.getText().toString().trim());
     if (!"https".equals(u.getScheme()) || u.getHost()==null || u.getRawUserInfo()!=null || u.getRawQuery()!=null || u.getRawFragment()!=null
       || !(u.getPath().isEmpty() || u.getPath().equals("/")) || name.getText().toString().trim().isEmpty()) throw new IllegalArgumentException();
     String url=u.toString().replaceAll("/$","");
     getSharedPreferences("servers",MODE_PRIVATE).edit().putString(name.getText().toString().trim(),url).apply(); remote(url);
    } catch (Exception e) { message("Enter a named HTTPS origin without credentials, path, query or fragment."); }
   }).show();
 }
 private void importFile() {
  new AlertDialog.Builder(this).setTitle("Import to this device").setItems(new String[]{"ROM file", "DAT checksums (.dat, .xml or .zip)"}, (d,n) -> pickImport(n == 0 ? 10 : 11)).show();
 }
 private void pickImport(int code) {
  Intent i=new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*");
  startActivityForResult(i,code);
 }
 @Override protected void onActivityResult(int request,int result,Intent data) {
  super.onActivityResult(request,result,data);
  if ((request!=10 && request!=11) || result!=RESULT_OK || data==null || data.getData()==null) return;
  Uri uri=data.getData();
  worker.execute(() -> {
   File partial=null;
   try {
    String name="import.rom";
    try (Cursor c=getContentResolver().query(uri,new String[]{OpenableColumns.DISPLAY_NAME},null,null,null)) {
     if (c!=null && c.moveToFirst()) name=c.getString(0);
    }
    name=name.replaceAll("[^a-zA-Z0-9._ ()-]","_");
    if (name.isEmpty() || name.equals(".") || name.equals("..")) name="import.rom";
    File folder=new File(getFilesDir(),request==11 ? "dats" : "library/Imported"); folder.mkdirs();
    File staging=new File(getFilesDir(),"staging"); staging.mkdirs();
    partial=File.createTempFile("import-",".partial",staging);
    try (InputStream in=getContentResolver().openInputStream(uri); OutputStream out=new FileOutputStream(partial)) {
     if (in==null) throw new IOException();
     byte[] buf=new byte[65536]; int n; while ((n=in.read(buf))!=-1) out.write(buf,0,n);
    }
    Python.getInstance().getModule("android_backend").callAttr("publish_import",getFilesDir().getAbsolutePath(),partial.getAbsolutePath(),name,request==11);
    if (request==11) Python.getInstance().getModule("android_backend").callAttr("refresh_dats",getFilesDir().getAbsolutePath());
    runOnUiThread(() -> message(request==11 ? "DAT copied and checksum catalogue reloaded. Check DAT status in ROMarr." : "Copied to private library/Imported. Scan this folder in ROMarr. Imports remain unverified until checked against a DAT."));
   } catch (Exception e) { runOnUiThread(() -> message("Import failed. Check storage and file access.")); }
   finally { if (partial!=null) partial.delete(); }
  });
 }
 private void chooseFile() {
  worker.execute(() -> {
   try {
    JSONArray rows=new JSONArray(Python.getInstance().getModule("android_backend").callAttr("library_files",getFilesDir().getAbsolutePath()).toString());
    String[] paths=new String[rows.length()]; for(int i=0;i<paths.length;i++) paths[i]=rows.getString(i);
    runOnUiThread(() -> { if (!destroyed) new AlertDialog.Builder(this).setTitle("Open in compatible emulator")
      .setItems(paths,(d,n) -> launch(paths[n])).setNegativeButton("Cancel",null).show(); });
   } catch (Exception e) { runOnUiThread(() -> message("Could not list local library.")); }
  });
 }
 private void launch(String path) {
  try {
   File root=new File(getFilesDir(),"library").getCanonicalFile(), f=new File(root,path).getCanonicalFile();
   if (!f.getPath().startsWith(root.getPath()+File.separator) || !f.isFile()) throw new IOException();
   Uri uri=FileProvider.getUriForFile(this,getPackageName()+".files",f);
   Intent i=new Intent(Intent.ACTION_VIEW).setDataAndType(uri,"application/octet-stream");
   i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION); i.setClipData(ClipData.newRawUri("ROMarr file",uri));
   startActivity(Intent.createChooser(i,"Open with compatible emulator"));
  } catch (ActivityNotFoundException e) { message("Install an emulator supporting ACTION_VIEW content URIs. Some require their own file picker."); }
  catch (Exception e) { message("Could not share this local file."); }
 }
 private void message(String text) { if (!destroyed) new AlertDialog.Builder(this).setMessage(text).setPositiveButton("OK",null).show(); }
 @Override protected void onDestroy() { destroyed=true; web.stopLoading(); web.destroy(); worker.shutdown(); super.onDestroy(); }
}
