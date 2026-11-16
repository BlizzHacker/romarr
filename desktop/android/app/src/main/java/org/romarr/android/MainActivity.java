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
 private WebViewClient navigationClient;
 private TextView status;
 private LinearLayout toolbar;
 private ImageButton actions;
 private volatile String origin = "";
 private String local = "";
 private volatile boolean destroyed;
 private volatile boolean wantLocal=true;
 private volatile int navigationGeneration;
 private final java.util.Set<String> signInOrigins=java.util.concurrent.ConcurrentHashMap.newKeySet();
 private boolean signingIn;
 private AlertDialog signInPrompt;
 private final java.util.concurrent.ExecutorService worker = java.util.concurrent.Executors.newSingleThreadExecutor();
 @Override public void onCreate(Bundle saved) {
  super.onCreate(saved);
  androidx.core.view.WindowCompat.setDecorFitsSystemWindows(getWindow(),false);
  LinearLayout layout = new LinearLayout(this); layout.setOrientation(LinearLayout.VERTICAL);
  layout.setBackgroundColor(android.graphics.Color.rgb(18,20,26));
  androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(layout,(v,insets) -> {
   androidx.core.graphics.Insets bars=insets.getInsets(androidx.core.view.WindowInsetsCompat.Type.systemBars() | androidx.core.view.WindowInsetsCompat.Type.displayCutout());
   androidx.core.graphics.Insets keyboard=insets.getInsets(androidx.core.view.WindowInsetsCompat.Type.ime());
   v.setPadding(bars.left,bars.top,bars.right,Math.max(bars.bottom,keyboard.bottom));
   return insets;
  });
  toolbar=new LinearLayout(this); toolbar.setGravity(android.view.Gravity.CENTER_VERTICAL);
  toolbar.setPadding(dp(14),dp(6),dp(6),dp(6)); toolbar.setMinimumHeight(dp(60));
  toolbar.setBackgroundColor(android.graphics.Color.rgb(26,29,37));
  ImageView mark=new ImageView(this);mark.setImageResource(R.drawable.ic_launcher);
  mark.setImportantForAccessibility(android.view.View.IMPORTANT_FOR_ACCESSIBILITY_NO);
  toolbar.addView(mark,new LinearLayout.LayoutParams(dp(32),dp(32)));
  LinearLayout identity=new LinearLayout(this);identity.setOrientation(LinearLayout.VERTICAL);
  identity.setGravity(android.view.Gravity.CENTER_VERTICAL);
  LinearLayout.LayoutParams identitySize=new LinearLayout.LayoutParams(0,-2,1);identitySize.setMargins(dp(10),0,dp(8),0);
  TextView brand=new TextView(this);brand.setText("ROMarr");brand.setTextSize(18);
  brand.setTextColor(android.graphics.Color.rgb(241,245,249));brand.setTypeface(null,android.graphics.Typeface.BOLD);
  brand.setSingleLine(true);identity.addView(brand);
  status=new TextView(this);status.setText("Starting on this device…");status.setTextSize(12);
  status.setTextColor(android.graphics.Color.rgb(169,183,198));status.setSingleLine(true);
  status.setEllipsize(android.text.TextUtils.TruncateAt.END);identity.addView(status);
  toolbar.addView(identity,identitySize);
  actions=new ImageButton(this);actions.setImageResource(R.drawable.ic_more);
  actions.setContentDescription("ROMarr menu");actions.setPadding(dp(12),dp(12),dp(12),dp(12));
  android.util.TypedValue ripple=new android.util.TypedValue();getTheme().resolveAttribute(android.R.attr.selectableItemBackgroundBorderless,ripple,true);
  if(ripple.resourceId!=0) actions.setBackgroundResource(ripple.resourceId);
  actions.setOnClickListener(v -> showActions());toolbar.addView(actions,new LinearLayout.LayoutParams(dp(48),dp(48)));
  layout.addView(toolbar,new LinearLayout.LayoutParams(-1,-2));
  web = new WebView(this);
  web.setFocusable(true); web.setFocusableInTouchMode(true);
  web.setOnTouchListener((view,event) -> {
   if(event.getActionMasked()==android.view.MotionEvent.ACTION_DOWN && !view.hasFocus()) view.requestFocus();
   return false;
  });web.setBackgroundColor(android.graphics.Color.rgb(18,20,26));
  if (BuildConfig.DEBUG) WebView.setWebContentsDebuggingEnabled(true);
  web.getSettings().setJavaScriptEnabled(true); web.getSettings().setDomStorageEnabled(true);
  web.getSettings().setAllowFileAccess(false); web.getSettings().setAllowContentAccess(false);
  web.getSettings().setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
  web.setWebViewClient(navigationClient = new WebViewClient() {
   @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
    return navigateRequest(r);
   }
   @Override public void onPageStarted(WebView v,String url,android.graphics.Bitmap icon) {
    String authority=httpsOrigin(url);
    if(!sameOrigin(url,origin) && (wantLocal || authority.isEmpty() || !signInOrigins.contains(authority))) {
     v.stopLoading();
     if(!wantLocal && !authority.isEmpty()) promptSignIn(url,authority,false);
    }
   }
   @Override public WebResourceResponse shouldInterceptRequest(WebView v,WebResourceRequest r) {
    return interceptMainFrame(r);
   }
   @Override public void onReceivedSslError(WebView v, android.webkit.SslErrorHandler h, android.net.http.SslError e) {
    h.cancel(); message("TLS certificate rejected. Use a trusted server certificate.");
   }
  });
  layout.addView(web,new LinearLayout.LayoutParams(-1,0,1)); setContentView(layout);
  androidx.core.view.WindowInsetsControllerCompat systemBars=new androidx.core.view.WindowInsetsControllerCompat(getWindow(),layout);
  systemBars.setAppearanceLightStatusBars(false);systemBars.setAppearanceLightNavigationBars(false);
  androidx.core.view.ViewCompat.requestApplyInsets(layout);
  getOnBackPressedDispatcher().addCallback(this, new androidx.activity.OnBackPressedCallback(true) {
   @Override public void handleOnBackPressed() { if (web.canGoBack()) web.goBack(); else finish(); }
  });
  worker.execute(() -> {
   try {
    String url=Python.getInstance().getModule("android_backend").callAttr("start",getFilesDir().getAbsolutePath()).toString();
    runOnUiThread(() -> { if (!destroyed) { local=url; if (wantLocal) local(); } });
   } catch (Exception e) {
    runOnUiThread(() -> { if (!destroyed && wantLocal) {status.setText("This device unavailable");status.setContentDescription("Local server unavailable. Servers remain available from the ROMarr menu.");} });
    android.util.Log.e("ROMarr","Local startup failed",e);
   }
  });
 }
 private int dp(int size) { return Math.round(size*getResources().getDisplayMetrics().density); }
 private void showActions() {
  PopupMenu menu=new PopupMenu(this,actions);
  menu.getMenu().add(0,1,0,"This device").setCheckable(true).setChecked(wantLocal);
  menu.getMenu().add(0,2,1,"Servers…");
  menu.getMenu().add(0,3,2,"Import ROM or DAT…");
  menu.getMenu().add(0,4,3,"Play a local file…");
  menu.getMenu().add(0,5,4,"Connection details");
  menu.getMenu().add(0,6,5,"Privacy policy");
  menu.setOnMenuItemClickListener(item -> {
   switch(item.getItemId()) {
    case 1: local();break;
    case 2: profiles();break;
    case 3: importFile();break;
    case 4: chooseFile();break;
    case 5: new AlertDialog.Builder(this).setTitle(wantLocal ? "This device" : "Server connection")
     .setMessage(wantLocal ? "Your private local library is available while ROMarr is running. Use Servers for an always-on library." : origin)
     .setPositiveButton("Done",null).show();break;
    case 6: openPrivacyPolicy();break;
    default:return false;
   }
   return true;
  });
  menu.show();
 }
 private void openPrivacyPolicy() {
  try { startActivity(new Intent(Intent.ACTION_VIEW,Uri.parse("https://romarr.app/privacy"))); }
  catch(ActivityNotFoundException e) { message("No browser installed. Privacy policy: https://romarr.app/privacy"); }
 }
 private static boolean sameOrigin(String a,String b) {
  try { URI x=new URI(a),y=new URI(b); return x.getScheme().equalsIgnoreCase(y.getScheme()) && x.getHost().equalsIgnoreCase(y.getHost()) && effectivePort(x)==effectivePort(y); }
  catch (Exception e) { return false; }
 }
 private static int effectivePort(URI uri) { return uri.getPort()>=0 ? uri.getPort() : ("https".equalsIgnoreCase(uri.getScheme()) ? 443 : 80); }
 private static String httpsOrigin(String url) {
  try {
   URI uri=new URI(url);
   if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost()==null || uri.getRawUserInfo()!=null) return "";
   return new URI("https",null,uri.getHost().toLowerCase(java.util.Locale.ROOT),effectivePort(uri)==443 ? -1 : uri.getPort(),null,null,null).toString();
  } catch(Exception e) { return ""; }
 }
 private static String profileOrigin(String url) throws java.net.URISyntaxException {
  URI uri=new URI(url.trim());
  String result=httpsOrigin(url.trim());
  if (result.isEmpty() || uri.getRawQuery()!=null || uri.getRawFragment()!=null
      || !(uri.getRawPath()==null || uri.getRawPath().isEmpty() || uri.getRawPath().equals("/"))
      || uri.getPort()==0 || uri.getPort()>65535) throw new IllegalArgumentException("Invalid HTTPS origin");
  return result;
 }
 private void clearSignIn() {
  if (signInPrompt!=null) {signInPrompt.dismiss();signInPrompt=null;}
  signInOrigins.clear(); signingIn=false;
 }
 private boolean navigateRequest(WebResourceRequest request) {
  String target=request.getUrl().toString();
  if (sameOrigin(target,origin)) {if(request.isForMainFrame()) signingIn=false;return false;}
  if (!request.isForMainFrame()) return true;
  String authority=httpsOrigin(target);
  if (!wantLocal && !authority.isEmpty()) {
   if (signInOrigins.contains(authority) && (request.isRedirect() || (signingIn && sameOrigin(target,web.getUrl())))) {signingIn=true;return false;}
   if (request.isRedirect()) {promptSignIn(target,authority);return true;}
  }
  if (request.isRedirect() && !wantLocal) {message("Sign-in redirects require HTTPS and a valid authority.");return true;}
  Uri uri=request.getUrl();
  if ("https".equals(uri.getScheme()) || "http".equals(uri.getScheme())) {
   try {startActivity(new Intent(Intent.ACTION_VIEW,uri));}
   catch(ActivityNotFoundException e) {message("No browser installed.");}
  }
  return true;
 }
 private WebResourceResponse interceptMainFrame(WebResourceRequest request) {
  if(!request.isForMainFrame()) return null;
  String target=request.getUrl().toString(),profile=origin;
  int generation=navigationGeneration;
  String authority=httpsOrigin(target);
  if(sameOrigin(target,profile) || (!wantLocal && !authority.isEmpty() && signInOrigins.contains(authority))) return null;
  // WebView does not invoke shouldOverrideUrlLoading for form POSTs.
  // Interception rejects their bytes before networking, on every HTTP verb.
  if(!destroyed && !wantLocal && !authority.isEmpty()) runOnUiThread(() -> {
   if(!destroyed && !wantLocal && generation==navigationGeneration && profile.equals(origin))
    promptSignIn(target,authority,"GET".equalsIgnoreCase(request.getMethod()));
  });
  byte[] body="<!doctype html><title>Sign-in approval required</title><p>This request was blocked. Approve the sign-in authority, then retry sign-in from your server.</p>".getBytes(java.nio.charset.StandardCharsets.UTF_8);
  return new WebResourceResponse("text/html","UTF-8",403,"Sign-in approval required",java.util.Collections.singletonMap("Cache-Control","no-store"),new ByteArrayInputStream(body));
 }
 private void promptSignIn(String target,String authority) { promptSignIn(target,authority,true); }
 private void promptSignIn(String target,String authority,boolean replayGet) {
  if (destroyed || wantLocal) return;
  if(signInPrompt!=null) signInPrompt.dismiss();
  final int generation=navigationGeneration;
  final String profile=origin;
  String host=Uri.parse(authority).getAuthority();
  signInPrompt=new AlertDialog.Builder(this).setTitle("Allow sign-in redirect?")
   .setMessage("Server " + Uri.parse(profile).getAuthority() + " requests HTTPS sign-in at " + host + ". Allow this authority inside ROMarr for the current server selection?" + (replayGet ? "" : " This form submission was blocked. Approval returns to your server; retry sign-in there. Form data is not replayed."))
   .setNegativeButton("Cancel",null).setPositiveButton("Allow sign-in",(d,w) -> {
    if (!destroyed && !wantLocal && generation==navigationGeneration && profile.equals(origin)) {
     signInOrigins.add(authority);signingIn=true;web.loadUrl(replayGet ? target : profile);
    }
   }).create();
  signInPrompt.show();
 }
 private void open(String url) {
  origin=url;web.loadUrl(url);
  status.setText(url.equals(local) ? "This device" : "Server · " + Uri.parse(url).getAuthority());
  status.setContentDescription(url.equals(local) ? "This device. Private local library." : "Connected server " + url + ". Full address in Connection details.");
 }
 private void local() {
  clearSignIn();
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
 private void remote(String url) {
  try { String canonical=profileOrigin(url); clearSignIn(); wantLocal=false; navigationGeneration++; open(canonical); }
  catch(Exception e) { message("Enter a valid HTTPS server origin."); }
 }
 private void profiles() {
  android.content.SharedPreferences prefs=getSharedPreferences("servers",MODE_PRIVATE);
  java.util.ArrayList<String> names=new java.util.ArrayList<>(prefs.getAll().keySet());
  java.util.Collections.sort(names); names.add("Add HTTPS server");
  new AlertDialog.Builder(this).setTitle("Server profiles").setItems(names.toArray(new String[0]),(d,n) -> {
   if (n==names.size()-1) { addProfile(); return; }
   String name=names.get(n);
   new AlertDialog.Builder(this).setTitle(name).setItems(new String[]{"Connect","Delete"},(x,a) -> {
    if (a==0) {
     try {String canonical=profileOrigin(prefs.getString(name,"")); prefs.edit().putString(name,canonical).apply(); remote(canonical);}
     catch(Exception e) {message("Saved server must be a valid HTTPS origin.");}
    } else prefs.edit().remove(name).apply();
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
     String url=profileOrigin(address.getText().toString());
     if (name.getText().toString().trim().isEmpty()) throw new IllegalArgumentException();
     getSharedPreferences("servers",MODE_PRIVATE).edit().putString(name.getText().toString().trim(),url).apply(); remote(url);
    } catch (Exception e) { message("Enter a named HTTPS origin without credentials, path, query or fragment."); }
   }).show();
 }
 private static String utf8Prefix(String value,int limit) {
  StringBuilder result=new StringBuilder(); int bytes=0;
  for(int offset=0;offset<value.length();) {
   int point=value.codePointAt(offset); String part=new String(Character.toChars(point));
   int count=part.getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
   if(bytes+count>limit) break;
   result.append(part); bytes+=count; offset+=Character.charCount(point);
  }
  return result.toString();
 }
 private static String importName(String display) {
  if(display==null) return "import.rom";
  StringBuilder cleaned=new StringBuilder();
  display.codePoints().forEach(point -> cleaned.appendCodePoint(point=='/' || point=='\\' || Character.isISOControl(point) ? '_' : point));
  String name=cleaned.toString();
  if(name.isEmpty() || name.equals(".") || name.equals("..")) return "import.rom";
  // Leave 13 bytes for the backend's collision prefix on 255-byte filesystems.
  if(name.getBytes(java.nio.charset.StandardCharsets.UTF_8).length>240) {
   int dot=name.lastIndexOf('.');
   String suffix=dot>0 ? name.substring(dot) : "";
   if(suffix.getBytes(java.nio.charset.StandardCharsets.UTF_8).length>32) suffix="";
   name=utf8Prefix(name,240-suffix.getBytes(java.nio.charset.StandardCharsets.UTF_8).length)+suffix;
  }
  return name;
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
    name=importName(name);
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
   for (String part:path.split("/")) if (part.toLowerCase(java.util.Locale.ROOT).startsWith(".romarr")) throw new IOException();
   File root=new File(getFilesDir(),"library").getCanonicalFile(), f=new File(root,path).getCanonicalFile();
   if (!f.getPath().startsWith(root.getPath()+File.separator) || !f.isFile()) throw new IOException();
   for (String part:f.getPath().substring(root.getPath().length()+1).split(java.util.regex.Pattern.quote(File.separator))) {
    if (part.toLowerCase(java.util.Locale.ROOT).startsWith(".romarr")) throw new IOException();
   }
   Uri uri=FileProvider.getUriForFile(this,getPackageName()+".files",f);
   Intent i=new Intent(Intent.ACTION_VIEW).setDataAndType(uri,"application/octet-stream");
   i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION); i.setClipData(ClipData.newRawUri("ROMarr file",uri));
   startActivity(Intent.createChooser(i,"Open with compatible emulator"));
  } catch (ActivityNotFoundException e) { message("Install an emulator supporting ACTION_VIEW content URIs. Some require their own file picker."); }
  catch (Exception e) { message("Could not share this local file."); }
 }
 private void message(String text) { if (!destroyed) new AlertDialog.Builder(this).setMessage(text).setPositiveButton("OK",null).show(); }
 @Override protected void onDestroy() { destroyed=true; clearSignIn(); web.stopLoading(); web.destroy(); worker.shutdown(); super.onDestroy(); }
}
