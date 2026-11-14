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
 @Test public void bundledServerAndStdlibWorkWithoutExternalPython() throws Exception {
  Context c=InstrumentationRegistry.getInstrumentation().getTargetContext();
  String base=Python.getInstance().getModule("android_backend").callAttr("start",c.getFilesDir().getAbsolutePath()).toString();
  assertTrue(base.startsWith("http://127.0.0.1:"));
  HttpURLConnection health=(HttpURLConnection)new URL(base+"/api/health").openConnection();
  health.setConnectTimeout(10000); health.setReadTimeout(10000);
  assertEquals(200,health.getResponseCode());
  String text=new String(health.getInputStream().readAllBytes(),java.nio.charset.StandardCharsets.UTF_8);
  assertTrue(text.contains("true"));
  HttpURLConnection ui=(HttpURLConnection)new URL(base+"/").openConnection();
  assertEquals(200,ui.getResponseCode());
  assertTrue(new String(ui.getInputStream().readAllBytes(),java.nio.charset.StandardCharsets.UTF_8).contains("ROMarr"));
  android.util.Log.i("ROMarrProof",Python.getInstance().getModule("android_backend").callAttr("runtime_facts").toString());
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
  try(InputStream in=c.getContentResolver().openInputStream(uri)) {assertArrayEquals(new byte[]{1,2,3},in.readAllBytes());}
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
  File source=new File(library,"native-import-test.rom");
  byte[] fixture=new byte[]{82,79,77,97,114,114};
  try(FileOutputStream out=new FileOutputStream(source)) {out.write(fixture);}
  File result=new File(library,"Imported/native-import-test.rom"); result.delete();
  android.net.Uri uri=FileProvider.getUriForFile(c,c.getPackageName()+".files",source);
  MainActivity activity=(MainActivity)inst.startActivitySync(new android.content.Intent(c,MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
  inst.runOnMainSync(() -> activity.onActivityResult(10,android.app.Activity.RESULT_OK,new android.content.Intent().setData(uri)));
  long deadline=android.os.SystemClock.elapsedRealtime()+30000;
  while(!result.exists() && android.os.SystemClock.elapsedRealtime()<deadline) Thread.sleep(100);
  assertTrue("Native copy completed",result.isFile());
  try(InputStream in=new FileInputStream(result)) {assertArrayEquals(fixture,in.readAllBytes());}
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
  try(InputStream in=new FileInputStream(original)) {assertArrayEquals(new byte[]{1,2,3},in.readAllBytes());}
  try(InputStream in=new FileInputStream(result)) {assertArrayEquals(new byte[]{4,5,6},in.readAllBytes());}
  assertFalse(staged.exists()); original.delete(); new File(result).delete();
 }

 @Test public void staleLocalCallbackCannotReplaceRemoteSelection() throws Exception {
  android.app.Instrumentation inst=InstrumentationRegistry.getInstrumentation(); Context c=inst.getTargetContext();
  MainActivity activity=(MainActivity)inst.startActivitySync(new android.content.Intent(c,MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
  java.lang.reflect.Method local=MainActivity.class.getDeclaredMethod("local"),remote=MainActivity.class.getDeclaredMethod("remote",String.class);
  local.setAccessible(true);remote.setAccessible(true);
  inst.runOnMainSync(() -> {
   try {local.invoke(activity); remote.invoke(activity,"https://romarr.example.invalid");}
   catch(Exception e) {throw new RuntimeException(e);}
  });
  Thread.sleep(2000); inst.waitForIdleSync();
  java.lang.reflect.Field field=MainActivity.class.getDeclaredField("origin");field.setAccessible(true);
  assertEquals("https://romarr.example.invalid",field.get(activity));
  inst.runOnMainSync(activity::finish);
 }

 @Test public void bundledCoreVersionMatchesAndroidPackage() throws Exception {
  assertEquals(BuildConfig.VERSION_NAME,Python.getInstance().getModule("android_backend").callAttr("core_version").toString());
  assertEquals("1.10.0-preview",BuildConfig.VERSION_NAME);
 }
}
