package org.romarr.android;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.core.content.FileProvider;
import android.content.Context;
import com.chaquo.python.Python;
import org.junit.Test;
import org.junit.runner.RunWith;
import static org.junit.Assert.*;
import java.io.*;
import java.net.*;

/** Device tests exercise the packaged interpreter, server and scoped sharing. */
@RunWith(AndroidJUnit4.class)
public class StandaloneTest {
 private static byte[] readFully(InputStream in) throws IOException {
  ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] buffer=new byte[4096];int n;
  while((n=in.read(buffer))!=-1) out.write(buffer,0,n);
  return out.toByteArray();
 }
 @Test public void bundledServerAndStdlibWorkWithoutExternalPython() throws Exception {
  Context c=InstrumentationRegistry.getInstrumentation().getTargetContext();
  String base=Python.getInstance().getModule("android_backend").callAttr("start",c.getFilesDir().getAbsolutePath()).toString();
  assertTrue(base.startsWith("http://127.0.0.1:"));
  HttpURLConnection health=(HttpURLConnection)new URL(base+"/api/health").openConnection();
  health.setConnectTimeout(10000); health.setReadTimeout(10000);
  assertEquals(200,health.getResponseCode());
  String text=new String(readFully(health.getInputStream()),java.nio.charset.StandardCharsets.UTF_8);
  assertTrue(text.contains("true"));
  HttpURLConnection ui=(HttpURLConnection)new URL(base+"/").openConnection();
  assertEquals(200,ui.getResponseCode());
  assertTrue(new String(readFully(ui.getInputStream()),java.nio.charset.StandardCharsets.UTF_8).contains("ROMarr"));
  String runtime=Python.getInstance().getModule("android_backend").callAttr("runtime_facts").toString();
  android.util.Log.i("ROMarrProof",runtime);
  org.json.JSONObject facts=new org.json.JSONObject(runtime);
  assertTrue("Private Context files anchor identity is verified",facts.getBoolean("files_anchor_verified"));
  assertTrue("Process-local automation scheduler is alive",facts.getBoolean("scheduler_alive"));
  java.util.Set<String> tasks=new java.util.HashSet<>();
  org.json.JSONArray jobs=facts.getJSONArray("tasks");
  for(int i=0;i<jobs.length();i++) tasks.add(jobs.getString(i));
  assertTrue(tasks.containsAll(java.util.Arrays.asList("ImportCompleted","MissingGameSearch","RssSync","FailedDownloads","ListSync")));
  HttpURLConnection denied=(HttpURLConnection)new URL(base+"/api/v1/config").openConnection();
  assertEquals(401,denied.getResponseCode());
  HttpURLConnection setup=(HttpURLConnection)new URL(base+"/api/v1/setup").openConnection();
  setup.setRequestMethod("POST"); setup.setDoOutput(true); setup.setRequestProperty("Content-Type","application/json");
  try(OutputStream out=setup.getOutputStream()) {out.write("{}".getBytes(java.nio.charset.StandardCharsets.UTF_8));}
  assertEquals(409,setup.getResponseCode());
  HttpURLConnection allowed=(HttpURLConnection)new URL(base+"/api/v1/config").openConnection();
  allowed.setRequestProperty("Cookie",Python.getInstance().getModule("android_backend").callAttr("session_cookie").toString().split(";",2)[0]);
  assertEquals(200,allowed.getResponseCode());
  assertEquals("ok",Python.getInstance().getModule("android_backend").callAttr("smoke_stdlib").toString());
 }
 @Test public void providerSharesOnlyLibraryWithGrant() throws Exception {
  Context c=InstrumentationRegistry.getInstrumentation().getTargetContext();
  File library=new File(c.getFilesDir(),"library"); library.mkdirs();
  File fixture=new File(library,"device-test.rom");
  try(FileOutputStream out=new FileOutputStream(fixture)) {out.write(new byte[]{1,2,3});}
  android.net.Uri uri=FileProvider.getUriForFile(c,c.getPackageName()+".files",fixture);
  try(InputStream in=c.getContentResolver().openInputStream(uri)) {assertArrayEquals(new byte[]{1,2,3},readFully(in));}
  try {
   FileProvider.getUriForFile(c,c.getPackageName()+".files",new File(c.getFilesDir(),"romarr.json"));
   fail("Private state must not be exported");
  } catch(IllegalArgumentException expected) {}
  fixture.delete();
 }

 @Test public void selectedDocumentCopiesThroughNativeImport() throws Exception {
  android.app.Instrumentation inst=InstrumentationRegistry.getInstrumentation();
  Context c=inst.getTargetContext();
  File library=new File(c.getFilesDir(),"library"); library.mkdirs();
  java.lang.reflect.Method normalize=MainActivity.class.getDeclaredMethod("importName",String.class);
  normalize.setAccessible(true);
  assertEquals("import.rom",normalize.invoke(null,(Object)null));
  assertEquals("bad_name__.rom",normalize.invoke(null,"bad/name\\\n.rom"));
  String longName=new String(new char[100]).replace("\0","界")+".rom";
  String shortened=(String)normalize.invoke(null,longName);
  assertTrue(shortened.endsWith(".rom"));
  assertTrue(shortened.getBytes(java.nio.charset.StandardCharsets.UTF_8).length<=240);
  File source=new File(library,"native-日本語, [v1] & test.rom");
  byte[] fixture=new byte[]{82,79,77,97,114,114};
  try(FileOutputStream out=new FileOutputStream(source)) {out.write(fixture);}
  File result=new File(library,"Imported/native-日本語, [v1] & test.rom"); result.delete();
  android.net.Uri uri=FileProvider.getUriForFile(c,c.getPackageName()+".files",source);
  MainActivity activity=(MainActivity)inst.startActivitySync(new android.content.Intent(c,MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
  inst.runOnMainSync(() -> activity.onActivityResult(10,android.app.Activity.RESULT_OK,new android.content.Intent().setData(uri)));
  long deadline=android.os.SystemClock.elapsedRealtime()+30000;
  while(!result.exists() && android.os.SystemClock.elapsedRealtime()<deadline) Thread.sleep(100);
  assertTrue("Native copy completed",result.isFile());
  try(InputStream in=new FileInputStream(result)) {assertArrayEquals(fixture,readFully(in));}
  source.delete(); result.delete(); inst.runOnMainSync(activity::finish);
 }


 @Test public void nativeWebViewAuthenticatesWithoutJavaScriptCredentialBridge() throws Exception {
  android.app.Instrumentation inst=InstrumentationRegistry.getInstrumentation();
  Context c=inst.getTargetContext();
  MainActivity activity=(MainActivity)inst.startActivitySync(new android.content.Intent(c,MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
  java.lang.reflect.Field field=MainActivity.class.getDeclaredField("web"); field.setAccessible(true);
  android.webkit.WebView web=(android.webkit.WebView)field.get(activity);
  long deadline=android.os.SystemClock.elapsedRealtime()+30000;
  java.util.concurrent.atomic.AtomicBoolean ready=new java.util.concurrent.atomic.AtomicBoolean();
  while(!ready.get() && android.os.SystemClock.elapsedRealtime()<deadline) {
   inst.runOnMainSync(() -> ready.set(web.getUrl()!=null && web.getUrl().startsWith("http://127.0.0.1:") && web.getProgress()==100));
   Thread.sleep(100);
  }
  assertTrue("Local WebView loaded",ready.get());
  inst.runOnMainSync(() -> web.evaluateJavascript("fetch('/api/v1/config').then(r=>window.__romarrProbe=String(r.status))",null));
  String response="";
  while(!response.equals("\"200\"") && android.os.SystemClock.elapsedRealtime()<deadline) {
   java.util.concurrent.CountDownLatch latch=new java.util.concurrent.CountDownLatch(1);
   java.util.concurrent.atomic.AtomicReference<String> value=new java.util.concurrent.atomic.AtomicReference<>("");
   inst.runOnMainSync(() -> web.evaluateJavascript("window.__romarrProbe || ''",r -> {value.set(r);latch.countDown();}));
   assertTrue(latch.await(5,java.util.concurrent.TimeUnit.SECONDS)); response=value.get(); Thread.sleep(100);
  }
  assertEquals("Authenticated API via native cookie","\"200\"",response);
  java.util.concurrent.CountDownLatch latch=new java.util.concurrent.CountDownLatch(1);
  java.util.concurrent.atomic.AtomicReference<String> visible=new java.util.concurrent.atomic.AtomicReference<>();
  inst.runOnMainSync(() -> web.evaluateJavascript("document.cookie.indexOf('romarr_session=')===-1",r -> {visible.set(r);latch.countDown();}));
  assertTrue(latch.await(5,java.util.concurrent.TimeUnit.SECONDS)); assertEquals("true",visible.get());
  inst.runOnMainSync(activity::finish);
 }

 @Test public void collidingImportNeverOverwritesExistingFile() throws Exception {
  Context c=InstrumentationRegistry.getInstrumentation().getTargetContext();
  File staging=new File(c.getFilesDir(),"staging"); staging.mkdirs();
  File staged=File.createTempFile("import-",".partial",staging);
  File folder=new File(c.getFilesDir(),"library/Imported"); folder.mkdirs();
  File original=new File(folder,"collision-test.rom");
  try(FileOutputStream out=new FileOutputStream(original)) {out.write(new byte[]{1,2,3});}
  try(FileOutputStream out=new FileOutputStream(staged)) {out.write(new byte[]{4,5,6});}
  String result=Python.getInstance().getModule("android_backend").callAttr("publish_import",c.getFilesDir().getAbsolutePath(),staged.getAbsolutePath(),original.getName(),false).toString();
  assertNotEquals(original.getAbsolutePath(),result);
  try(InputStream in=new FileInputStream(original)) {assertArrayEquals(new byte[]{1,2,3},readFully(in));}
  try(InputStream in=new FileInputStream(result)) {assertArrayEquals(new byte[]{4,5,6},readFully(in));}
  assertFalse(staged.exists()); original.delete(); new File(result).delete();
 }

 @Test public void staleLocalCallbackCannotReplaceRemoteSelection() throws Exception {
  android.app.Instrumentation inst=InstrumentationRegistry.getInstrumentation(); Context c=inst.getTargetContext();
  MainActivity activity=(MainActivity)inst.startActivitySync(new android.content.Intent(c,MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
  java.lang.reflect.Method local=MainActivity.class.getDeclaredMethod("local"),remote=MainActivity.class.getDeclaredMethod("remote",String.class);
  local.setAccessible(true);remote.setAccessible(true);
  java.lang.reflect.Field localField=MainActivity.class.getDeclaredField("local"),workerField=MainActivity.class.getDeclaredField("worker");
  localField.setAccessible(true);workerField.setAccessible(true);
  long ready=android.os.SystemClock.elapsedRealtime()+30000;
  while(((String)localField.get(activity)).isEmpty() && android.os.SystemClock.elapsedRealtime()<ready) {inst.waitForIdleSync();Thread.sleep(25);}
  assertFalse("Startup delivered a usable local listener",((String)localField.get(activity)).isEmpty());
  java.util.concurrent.ExecutorService worker=(java.util.concurrent.ExecutorService)workerField.get(activity);
  java.util.concurrent.CountDownLatch block=new java.util.concurrent.CountDownLatch(1),entered=new java.util.concurrent.CountDownLatch(1);
  java.util.concurrent.Future<?> held=worker.submit(() -> {entered.countDown();try{block.await(10,java.util.concurrent.TimeUnit.SECONDS);}catch(InterruptedException e){Thread.currentThread().interrupt();}});
  assertTrue(entered.await(5,java.util.concurrent.TimeUnit.SECONDS));
  inst.runOnMainSync(() -> {
   try {local.invoke(activity); remote.invoke(activity,"https://romarr.example.invalid");}
   catch(Exception e) {throw new RuntimeException(e);}
  });
  block.countDown();held.get(10,java.util.concurrent.TimeUnit.SECONDS);
  worker.submit(() -> {}).get(10,java.util.concurrent.TimeUnit.SECONDS);inst.waitForIdleSync();
  java.lang.reflect.Field field=MainActivity.class.getDeclaredField("origin");field.setAccessible(true);
  assertEquals("https://romarr.example.invalid",field.get(activity));
  inst.runOnMainSync(activity::finish);
 }

 @Test public void compactToolbarAndWebViewFitTheUsableWindowAndKeyboard() throws Exception {
  android.app.Instrumentation inst=InstrumentationRegistry.getInstrumentation();Context context=inst.getTargetContext();
  MainActivity activity=(MainActivity)inst.startActivitySync(new android.content.Intent(context,MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
  java.lang.reflect.Field browserField=MainActivity.class.getDeclaredField("web"),barField=MainActivity.class.getDeclaredField("toolbar"),actionsField=MainActivity.class.getDeclaredField("actions"),statusField=MainActivity.class.getDeclaredField("status"),localField=MainActivity.class.getDeclaredField("local"),originField=MainActivity.class.getDeclaredField("origin");
  for(java.lang.reflect.Field field:new java.lang.reflect.Field[]{browserField,barField,actionsField,statusField,localField,originField}) field.setAccessible(true);
  android.webkit.WebView browser=(android.webkit.WebView)browserField.get(activity);
  android.view.View bar=(android.view.View)barField.get(activity),actions=(android.view.View)actionsField.get(activity);
  android.widget.TextView status=(android.widget.TextView)statusField.get(activity);
  long ready=android.os.SystemClock.elapsedRealtime()+30000;
  while((localField.get(activity).toString().isEmpty() || !localField.get(activity).equals(originField.get(activity))) && android.os.SystemClock.elapsedRealtime()<ready) {Thread.sleep(25);inst.waitForIdleSync();}
  assertFalse("Local server is ready",localField.get(activity).toString().isEmpty());
  inst.runOnMainSync(() -> assertNativeBounds(activity,bar,actions,status,browser));
  captureToolbarProof(inst,context,"local");
  java.lang.reflect.Method remote=MainActivity.class.getDeclaredMethod("remote",String.class);remote.setAccessible(true);
  String address="https://a-very-long-server-name-for-mobile-layout.example.invalid";
  inst.runOnMainSync(() -> {try{remote.invoke(activity,address);}catch(Exception e){throw new RuntimeException(e);}});
  inst.waitForIdleSync();
  inst.runOnMainSync(() -> {
   assertTrue(status.getContentDescription().toString().contains(address));
   assertEquals(1,status.getLineCount());assertNativeBounds(activity,bar,actions,status,browser);
   if(status.getPaint().measureText(status.getText().toString())>status.getWidth()) assertTrue("Long server name ellipsizes instead of clipping controls",status.getLayout().getEllipsisCount(0)>0);
  });
  captureToolbarProof(inst,context,"remote");
  java.lang.reflect.Method local=MainActivity.class.getDeclaredMethod("local");local.setAccessible(true);
  inst.runOnMainSync(() -> {try{local.invoke(activity);}catch(Exception e){throw new RuntimeException(e);}});
  while(!localField.get(activity).equals(originField.get(activity)) && android.os.SystemClock.elapsedRealtime()<ready) {Thread.sleep(25);inst.waitForIdleSync();}
  String base=localField.get(activity).toString();
  inst.runOnMainSync(() -> browser.loadDataWithBaseURL(base,"<meta name='viewport' content='width=device-width,initial-scale=1'><body style='margin:0'><input id='proof' style='width:95%;height:48px;font-size:18px' aria-label='Keyboard proof'></body>","text/html","UTF-8",null));
  java.util.concurrent.atomic.AtomicBoolean inputReady=new java.util.concurrent.atomic.AtomicBoolean();
  long deadline=android.os.SystemClock.elapsedRealtime()+10000;
  while(!inputReady.get() && android.os.SystemClock.elapsedRealtime()<deadline) {
   java.util.concurrent.CountDownLatch checked=new java.util.concurrent.CountDownLatch(1);
   inst.runOnMainSync(() -> browser.evaluateJavascript("!!document.getElementById('proof')",value -> {inputReady.set("true".equals(value));checked.countDown();}));
   assertTrue(checked.await(5,java.util.concurrent.TimeUnit.SECONDS));Thread.sleep(25);
  }
  assertTrue("WebView input fixture loaded",inputReady.get());
  int[] point=new int[2];inst.runOnMainSync(() -> browser.getLocationOnScreen(point));
  float density=context.getResources().getDisplayMetrics().density;
  long touch=android.os.SystemClock.uptimeMillis();
  android.view.MotionEvent down=android.view.MotionEvent.obtain(touch,touch,android.view.MotionEvent.ACTION_DOWN,point[0]+24*density,point[1]+24*density,0);
  android.view.MotionEvent up=android.view.MotionEvent.obtain(touch,touch+30,android.view.MotionEvent.ACTION_UP,point[0]+24*density,point[1]+24*density,0);
  inst.sendPointerSync(down);inst.sendPointerSync(up);down.recycle();up.recycle();
  java.util.concurrent.CountDownLatch focused=new java.util.concurrent.CountDownLatch(1);
  java.util.concurrent.atomic.AtomicBoolean fieldFocused=new java.util.concurrent.atomic.AtomicBoolean();
  inst.runOnMainSync(() -> {
   browser.requestFocus();
   browser.evaluateJavascript("(function(){const field=document.getElementById('proof');field.focus();return document.activeElement===field;})()",value -> {fieldFocused.set("true".equals(value));focused.countDown();});
  });
  assertTrue(focused.await(5,java.util.concurrent.TimeUnit.SECONDS));assertTrue("WebView text input owns focus",fieldFocused.get());
  java.util.concurrent.atomic.AtomicBoolean editorReady=new java.util.concurrent.atomic.AtomicBoolean();
  deadline=android.os.SystemClock.elapsedRealtime()+10000;
  while(!editorReady.get() && android.os.SystemClock.elapsedRealtime()<deadline) {
   inst.runOnMainSync(() -> editorReady.set(browser.hasFocus()&&browser.onCheckIsTextEditor()));Thread.sleep(25);
  }
  assertTrue("Native WebView input connection is ready",editorReady.get());
  inst.runOnMainSync(() -> {android.view.inputmethod.InputMethodManager manager=(android.view.inputmethod.InputMethodManager)activity.getSystemService(Context.INPUT_METHOD_SERVICE);manager.showSoftInput(browser,android.view.inputmethod.InputMethodManager.SHOW_IMPLICIT);});
  deadline=android.os.SystemClock.elapsedRealtime()+10000;
  java.util.concurrent.atomic.AtomicBoolean keyboard=new java.util.concurrent.atomic.AtomicBoolean();
  while(!keyboard.get() && android.os.SystemClock.elapsedRealtime()<deadline) {
   inst.runOnMainSync(() -> {androidx.core.view.WindowInsetsCompat insets=androidx.core.view.ViewCompat.getRootWindowInsets(browser);keyboard.set(insets!=null&&insets.isVisible(androidx.core.view.WindowInsetsCompat.Type.ime()));});Thread.sleep(25);
  }
  assertTrue("Real WebView focus opens the keyboard",keyboard.get());
  inst.getUiAutomation().waitForIdle(300,5000);inst.waitForIdleSync();inst.runOnMainSync(() -> assertNativeBounds(activity,bar,actions,status,browser));
  captureToolbarProof(inst,context,"keyboard");
  inst.runOnMainSync(() -> {((android.view.inputmethod.InputMethodManager)activity.getSystemService(Context.INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(browser.getWindowToken(),0);activity.finish();});
 }
 private static void captureToolbarProof(android.app.Instrumentation inst,Context context,String mode) throws IOException {
  if(!"true".equals(InstrumentationRegistry.getArguments().getString("toolbarProof","false"))) return;
  android.graphics.Bitmap screenshot=inst.getUiAutomation().takeScreenshot();assertNotNull(screenshot);
  try(FileOutputStream out=new FileOutputStream(new File(context.getCacheDir(),"toolbar-"+mode+".jpg"))) {assertTrue(screenshot.compress(android.graphics.Bitmap.CompressFormat.JPEG,85,out));}
  screenshot.recycle();
 }
 private static void assertNativeBounds(MainActivity activity,android.view.View bar,android.view.View actions,android.widget.TextView status,android.view.View browser) {
  android.graphics.Rect usable=new android.graphics.Rect(),toolbarBounds=new android.graphics.Rect(),menuBounds=new android.graphics.Rect(),webBounds=new android.graphics.Rect();
  activity.getWindow().getDecorView().getWindowVisibleDisplayFrame(usable);
  assertTrue(bar.getGlobalVisibleRect(toolbarBounds));assertTrue(actions.getGlobalVisibleRect(menuBounds));assertTrue(browser.getGlobalVisibleRect(webBounds));
  assertTrue("Toolbar stays inside system/keyboard insets",usable.contains(toolbarBounds));
  assertTrue("Menu remains fully visible",usable.contains(menuBounds));
  assertEquals(actions.getWidth(),menuBounds.width());assertEquals(actions.getHeight(),menuBounds.height());
  assertTrue("WebView sits below the native toolbar",webBounds.top>=toolbarBounds.bottom);
  assertTrue("WebView ends above system/keyboard insets",webBounds.bottom<=usable.bottom);
  assertEquals("WebView is resized, not clipped behind keyboard",browser.getHeight(),webBounds.height());
  assertTrue("WebView retains useful space",browser.getHeight()>activity.getResources().getDisplayMetrics().density*24);
  assertTrue("Menu touch target remains at least48dp",actions.getWidth()>=activity.getResources().getDisplayMetrics().density*48-1);
  assertEquals("ROMarr menu",actions.getContentDescription().toString());
  assertTrue("Server label has measured space",status.getWidth()>0);
 }

 @Test public void bundledCoreVersionMatchesAndroidPackage() throws Exception {
  assertEquals(BuildConfig.VERSION_NAME,Python.getInstance().getModule("android_backend").callAttr("core_version").toString());
 }

 private static android.webkit.WebResourceRequest request(String url,boolean redirect) {
  return new android.webkit.WebResourceRequest() {
   public android.net.Uri getUrl(){return android.net.Uri.parse(url);}
   public boolean isForMainFrame(){return true;}
   public boolean isRedirect(){return redirect;}
   public boolean hasGesture(){return !redirect;}
   public String getMethod(){return "GET";}
   public java.util.Map<String,String> getRequestHeaders(){return java.util.Collections.emptyMap();}
  };
 }
 @Test public void signInRedirectRequiresAuthorityConsentAndRejectsStaleCallbacks() throws Exception {
  java.lang.reflect.Method same=MainActivity.class.getDeclaredMethod("sameOrigin",String.class,String.class);
  same.setAccessible(true);
  assertEquals(true,same.invoke(null,"HTTPS://Example.COM:443/a","https://example.com/b"));
  assertEquals(false,same.invoke(null,"https://example.com:444/a","https://example.com/b"));
  java.lang.reflect.Method canonical=MainActivity.class.getDeclaredMethod("profileOrigin",String.class);
  canonical.setAccessible(true);
  assertEquals("https://example.com",canonical.invoke(null,"HTTPS://Example.COM:443/"));
  android.app.Instrumentation inst=InstrumentationRegistry.getInstrumentation();Context c=inst.getTargetContext();
  MainActivity activity=(MainActivity)inst.startActivitySync(new android.content.Intent(c,MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
  java.lang.reflect.Method remote=MainActivity.class.getDeclaredMethod("remote",String.class),nav=MainActivity.class.getDeclaredMethod("navigateRequest",android.webkit.WebResourceRequest.class);
  remote.setAccessible(true);nav.setAccessible(true);
  java.lang.reflect.Field prompt=MainActivity.class.getDeclaredField("signInPrompt"),origin=MainActivity.class.getDeclaredField("origin"),approved=MainActivity.class.getDeclaredField("signInOrigins");
  prompt.setAccessible(true);origin.setAccessible(true);approved.setAccessible(true);
  inst.runOnMainSync(() -> {
   try {
    remote.invoke(activity,"https://server.example.invalid");
    assertEquals(true,nav.invoke(activity,request("https://identity.example.invalid/login?private=value",true)));
    android.app.AlertDialog dialog=(android.app.AlertDialog)prompt.get(activity);
    String message=((android.widget.TextView)dialog.findViewById(android.R.id.message)).getText().toString();
    assertTrue(message.contains("identity.example.invalid"));assertFalse(message.contains("private"));assertFalse(message.contains("/login"));
    dialog.getButton(android.app.AlertDialog.BUTTON_POSITIVE).performClick();
   } catch(Exception e){throw new RuntimeException(e);}
  });
  inst.waitForIdleSync();
  inst.runOnMainSync(() -> {
   try {
    assertTrue(((java.util.Set<?>)approved.get(activity)).contains("https://identity.example.invalid"));
    assertEquals(false,nav.invoke(activity,request("https://identity.example.invalid/next",true)));
    assertEquals(false,nav.invoke(activity,request("https://server.example.invalid/",true)));
   } catch(Exception e){throw new RuntimeException(e);}
  });
  android.content.IntentFilter externalFilter=new android.content.IntentFilter(android.content.Intent.ACTION_VIEW);externalFilter.addDataScheme("https");
  android.app.Instrumentation.ActivityMonitor monitor=new android.app.Instrumentation.ActivityMonitor(externalFilter,new android.app.Instrumentation.ActivityResult(0,null),true);
  inst.addMonitor(monitor);
  inst.runOnMainSync(() -> {
   try {assertEquals(true,nav.invoke(activity,request("https://identity.example.invalid/help",false)));}
   catch(Exception e){throw new RuntimeException(e);}
  });
  assertEquals(1,monitor.getHits());inst.removeMonitor(monitor);
  inst.runOnMainSync(() -> {
   try {
    nav.invoke(activity,request("https://other-identity.example.invalid/login?private=another",true));
    android.app.AlertDialog old=(android.app.AlertDialog)prompt.get(activity);
    android.widget.Button allow=old.getButton(android.app.AlertDialog.BUTTON_POSITIVE);
    remote.invoke(activity,"https://next-server.example.invalid");allow.performClick();
    assertEquals("https://next-server.example.invalid",origin.get(activity));assertTrue(((java.util.Set<?>)approved.get(activity)).isEmpty());
   } catch(Exception e){throw new RuntimeException(e);}
  });
  inst.waitForIdleSync();
  assertEquals("https://next-server.example.invalid",origin.get(activity));
  assertTrue(((java.util.Set<?>)approved.get(activity)).isEmpty());
  proveAutoPostRequiresConsent(inst,activity);
  String selectedServer=origin.get(activity).toString();
  android.content.IntentFilter privacyFilter=new android.content.IntentFilter(android.content.Intent.ACTION_VIEW);
  privacyFilter.addDataScheme("https");privacyFilter.addDataAuthority("romarr.app",null);
  privacyFilter.addDataPath("/privacy",android.os.PatternMatcher.PATTERN_LITERAL);
  android.app.Instrumentation.ActivityMonitor privacyMonitor=new android.app.Instrumentation.ActivityMonitor(privacyFilter,new android.app.Instrumentation.ActivityResult(0,null),true);
  inst.addMonitor(privacyMonitor);
  java.lang.reflect.Method privacy=MainActivity.class.getDeclaredMethod("openPrivacyPolicy");privacy.setAccessible(true);
  inst.runOnMainSync(() -> {try{privacy.invoke(activity);}catch(Exception e){throw new RuntimeException(e);}});
  assertEquals("Privacy policy opens the known HTTPS page in an external browser",1,privacyMonitor.getHits());
  assertEquals("Privacy does not change the selected server",selectedServer,origin.get(activity));
  inst.removeMonitor(privacyMonitor);
  inst.runOnMainSync(activity::finish);
 }

 private static void proveAutoPostRequiresConsent(android.app.Instrumentation inst,MainActivity activity) throws Exception {
  java.lang.reflect.Method remote=MainActivity.class.getDeclaredMethod("remote",String.class);remote.setAccessible(true);
  java.lang.reflect.Field webField=MainActivity.class.getDeclaredField("web"),promptField=MainActivity.class.getDeclaredField("signInPrompt");
  webField.setAccessible(true);promptField.setAccessible(true);
  android.webkit.WebView web=(android.webkit.WebView)webField.get(activity);
  java.util.concurrent.atomic.AtomicInteger rendered=new java.util.concurrent.atomic.AtomicInteger();
  java.util.concurrent.atomic.AtomicReference<String> destinationMethod=new java.util.concurrent.atomic.AtomicReference<>("");
  String form="<html><body><form method='POST' action='https://form-identity.example.invalid/consume'><input name='fixture' value='harmless'></form><script>document.forms[0].submit()</script></body></html>";
  inst.runOnMainSync(() -> {
   try {
    remote.invoke(activity,"https://form-server.example.invalid");
    java.lang.reflect.Field clientField=MainActivity.class.getDeclaredField("navigationClient");clientField.setAccessible(true);
    android.webkit.WebViewClient production=(android.webkit.WebViewClient)clientField.get(activity);
    web.setWebViewClient(new android.webkit.WebViewClient() {
     @Override public boolean shouldOverrideUrlLoading(android.webkit.WebView v,android.webkit.WebResourceRequest r) {return production.shouldOverrideUrlLoading(v,r);}
     @Override public void onPageStarted(android.webkit.WebView v,String url,android.graphics.Bitmap icon) {production.onPageStarted(v,url,icon);}
     @Override public android.webkit.WebResourceResponse shouldInterceptRequest(android.webkit.WebView v,android.webkit.WebResourceRequest r) {
      android.webkit.WebResourceResponse blocked=production.shouldInterceptRequest(v,r);
      if(blocked!=null) return blocked;
      if("form-identity.example.invalid".equals(r.getUrl().getHost())) {
       if(!r.isForMainFrame()) return new android.webkit.WebResourceResponse("text/plain","UTF-8",new ByteArrayInputStream(new byte[0]));
       destinationMethod.set(r.getMethod());rendered.incrementAndGet();
       return new android.webkit.WebResourceResponse("text/html","UTF-8",new ByteArrayInputStream("<html><title>Approved destination</title><body>approved-destination</body></html>".getBytes(java.nio.charset.StandardCharsets.UTF_8)));
      }
      return null;
     }
    });
    web.loadDataWithBaseURL("https://form-server.example.invalid",form,"text/html","UTF-8",null);
   } catch(Exception e){throw new RuntimeException(e);}
  });
  java.util.concurrent.atomic.AtomicBoolean consent=new java.util.concurrent.atomic.AtomicBoolean();
  long deadline=android.os.SystemClock.elapsedRealtime()+30000;
  while(!consent.get() && android.os.SystemClock.elapsedRealtime()<deadline) {
   inst.runOnMainSync(() -> {try{android.app.AlertDialog dialog=(android.app.AlertDialog)promptField.get(activity);consent.set(dialog!=null&&dialog.isShowing());}catch(Exception e){throw new RuntimeException(e);}});
   Thread.sleep(25);
  }
  assertTrue("Auto-submitted POST requires native authority consent",consent.get());
  assertEquals("Destination was not served before consent",0,rendered.get());
  java.util.concurrent.atomic.AtomicReference<String> title=new java.util.concurrent.atomic.AtomicReference<>("");
  java.util.concurrent.CountDownLatch probed=new java.util.concurrent.CountDownLatch(1);
  inst.runOnMainSync(() -> web.evaluateJavascript("document.title",value -> {title.set(value);probed.countDown();}));
  assertTrue(probed.await(5,java.util.concurrent.TimeUnit.SECONDS));assertNotEquals("\"Approved destination\"",title.get());
  inst.runOnMainSync(() -> {try{((android.app.AlertDialog)promptField.get(activity)).getButton(android.app.AlertDialog.BUTTON_POSITIVE).performClick();}catch(Exception e){throw new RuntimeException(e);}});
  inst.waitForIdleSync();
  inst.runOnMainSync(() -> web.loadDataWithBaseURL("https://form-server.example.invalid",form,"text/html","UTF-8",null));
  while(rendered.get()==0 && android.os.SystemClock.elapsedRealtime()<deadline) Thread.sleep(25);
  assertEquals("Explicitly retried approved POST reaches destination",1,rendered.get());
  assertEquals("POST",destinationMethod.get());
 }
 @Test public void quarantinedAndInternalFilesCannotBeOfferedOrHandedOff() throws Exception {
  android.app.Instrumentation inst=InstrumentationRegistry.getInstrumentation();Context c=inst.getTargetContext();
  File root=new File(c.getFilesDir(),"library"),quarantine=new File(root,".romarr-file-quarantine/test.rom"),internal=new File(root,"nested/.romarr-internal/test.rom");
  quarantine.getParentFile().mkdirs();internal.getParentFile().mkdirs();
  try(FileOutputStream out=new FileOutputStream(quarantine)){out.write(1);}
  try(FileOutputStream out=new FileOutputStream(internal)){out.write(2);}
  String files=Python.getInstance().getModule("android_backend").callAttr("library_files",c.getFilesDir().getAbsolutePath()).toString();
  assertFalse(files.contains(".romarr"));
  MainActivity activity=(MainActivity)inst.startActivitySync(new android.content.Intent(c,MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
  java.lang.reflect.Method launch=MainActivity.class.getDeclaredMethod("launch",String.class);launch.setAccessible(true);
  android.app.Instrumentation.ActivityMonitor monitor=new android.app.Instrumentation.ActivityMonitor(new android.content.IntentFilter(android.content.Intent.ACTION_CHOOSER),new android.app.Instrumentation.ActivityResult(0,null),true);inst.addMonitor(monitor);
  inst.runOnMainSync(() -> {try{launch.invoke(activity,".romarr-file-quarantine/test.rom");}catch(Exception e){throw new RuntimeException(e);}});
  assertEquals(0,monitor.getHits());inst.removeMonitor(monitor);inst.runOnMainSync(activity::finish);
  quarantine.delete();internal.delete();
 }

 @Test public void reportQuarantineRestoreWorksInAndroidPrivateStorage() {
  android.content.Context c=InstrumentationRegistry.getInstrumentation().getTargetContext();
  assertEquals("ok",Python.getInstance().getModule("android_backend").callAttr("smoke_report_quarantine_restore",c.getFilesDir().getAbsolutePath()).toString());
 }
}
