package org.romarr.android;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.Test;
import org.junit.runner.RunWith;
import static org.junit.Assert.*;

/** Explicit live proof stops at the identity provider's public login page. */
@RunWith(AndroidJUnit4.class)
public class LiveSsoTest {
 @Test public void activeServerShowsIdentityLoginWithoutCredentials() throws Exception {
  org.junit.Assume.assumeTrue("Live check is opt-in",Boolean.parseBoolean(InstrumentationRegistry.getArguments().getString("liveSso","false")));
  android.app.Instrumentation inst=InstrumentationRegistry.getInstrumentation();android.content.Context c=inst.getTargetContext();
  MainActivity activity=(MainActivity)inst.startActivitySync(new android.content.Intent(c,MainActivity.class).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK));
  java.lang.reflect.Method remote=MainActivity.class.getDeclaredMethod("remote",String.class);remote.setAccessible(true);
  java.lang.reflect.Field prompt=MainActivity.class.getDeclaredField("signInPrompt"),browser=MainActivity.class.getDeclaredField("web");prompt.setAccessible(true);browser.setAccessible(true);
  android.webkit.WebView web=(android.webkit.WebView)browser.get(activity);
  inst.runOnMainSync(() -> {try{remote.invoke(activity,"https://romarr.moveweight.com");}catch(Exception e){throw new RuntimeException(e);}});
  java.util.concurrent.atomic.AtomicBoolean consent=new java.util.concurrent.atomic.AtomicBoolean();
  long deadline=android.os.SystemClock.elapsedRealtime()+60000;
  while(!consent.get() && android.os.SystemClock.elapsedRealtime()<deadline) {
   inst.runOnMainSync(() -> {
    try {
     android.app.AlertDialog d=(android.app.AlertDialog)prompt.get(activity);
     if(d!=null && d.isShowing()) {
      String message=((android.widget.TextView)d.findViewById(android.R.id.message)).getText().toString();
      assertTrue(message.contains("authentik.moveweight.com"));assertFalse(message.contains("next="));assertFalse(message.contains("/if/"));
      d.getButton(android.app.AlertDialog.BUTTON_POSITIVE).performClick();consent.set(true);
     }
    }catch(Exception e){throw new RuntimeException(e);}
   });Thread.sleep(100);
  }
  assertTrue("Native HTTPS authority consent appeared",consent.get());
  java.util.concurrent.atomic.AtomicReference<String> probe=new java.util.concurrent.atomic.AtomicReference<>("");
  while(!"true".equals(probe.get()) && android.os.SystemClock.elapsedRealtime()<deadline) {
   java.util.concurrent.CountDownLatch latch=new java.util.concurrent.CountDownLatch(1);
   inst.runOnMainSync(() -> web.evaluateJavascript("(function(){function hasInput(r){if(Array.from(r.querySelectorAll('input')).some(e=>['text','email','password'].includes(e.type)))return true;return Array.from(r.querySelectorAll('*')).some(e=>e.shadowRoot&&hasInput(e.shadowRoot));}return location.hostname==='authentik.moveweight.com'&&hasInput(document);})()",v -> {probe.set(v);latch.countDown();}));
   assertTrue(latch.await(5,java.util.concurrent.TimeUnit.SECONDS));Thread.sleep(250);
  }
  assertEquals("Identity login inputs rendered","true",probe.get());
  android.util.Log.i("ROMarrLiveProof","HTTPS identity authority authentik.moveweight.com rendered login inputs; no credentials entered");
  inst.runOnMainSync(activity::finish);
 }
}
