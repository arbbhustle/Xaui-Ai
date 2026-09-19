package com.arbnor.xauai

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.drawable.AdaptiveIconDrawable
import android.graphics.drawable.BitmapDrawable
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File

@RunWith(RobolectricTestRunner::class)
@Config(sdk=[35],qualifiers="xxhdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class LauncherIconTest {
    private fun checkLauncher() {
        val app=RuntimeEnvironment.getApplication()
        assertEquals(R.mipmap.ic_launcher,app.applicationInfo.icon)
        assertEquals("DardaniaXAUTRADE AI",app.packageManager.getApplicationLabel(app.applicationInfo).toString())
        // Resolve the manifest resource directly; Robolectric's package-manager
        // shadow substitutes a default drawable for getApplicationIcon().
        assertTrue(app.getDrawable(app.applicationInfo.icon) is AdaptiveIconDrawable)
        for(id in listOf(R.mipmap.ic_launcher,R.mipmap.ic_launcher_round)) {
            val icon=app.getDrawable(id) as AdaptiveIconDrawable
            assertNotNull(icon.background)
            val foreground=icon.foreground as BitmapDrawable
            val bitmap=foreground.bitmap
            // Intact 48dp artwork is centered in a 108dp transparent layer.
            assertEquals(bitmap.width,bitmap.height)
            assertEquals(0,android.graphics.Color.alpha(bitmap.getPixel(0,0)))
            assertTrue(android.graphics.Color.alpha(bitmap.getPixel(bitmap.width/2,bitmap.height/2))>0)
            for(y in 0 until bitmap.height) for(x in 0 until bitmap.width) {
                if(android.graphics.Color.alpha(bitmap.getPixel(x,y))>0) {
                    assertTrue(x>=bitmap.width*30/108-1 && x<=bitmap.width*78/108+1)
                    assertTrue(y>=bitmap.height*30/108-1 && y<=bitmap.height*78/108+1)
                }
            }
            for(size in listOf(144,192)) {
                val preview=Bitmap.createBitmap(size,size,Bitmap.Config.ARGB_8888)
                icon.setBounds(0,0,size,size); icon.draw(Canvas(preview))
                val file=File("build/reports/phase4a-ui/launcher-${android.os.Build.VERSION.SDK_INT}-$id-$size.png")
                file.parentFile?.mkdirs(); file.outputStream().use { preview.compress(Bitmap.CompressFormat.PNG,100,it) }
                preview.recycle()
            }
        }
    }
    @Test fun approvedAdaptiveIdentityAtS23LauncherSizes() { checkLauncher() }
    @Test @Config(sdk=[26]) fun adaptiveIdentityOnMinimumAndroid() { checkLauncher() }
    @Test @Config(sdk=[31]) fun android12AdaptiveIdentity() { checkLauncher() }
    @Test fun android12SplashUsesApprovedLauncherResource() {
        val app=RuntimeEnvironment.getApplication()
        val theme=app.resources.newTheme().apply { applyStyle(R.style.Theme_XAUAI,true) }
        val value=android.util.TypedValue()
        assertTrue(theme.resolveAttribute(android.R.attr.windowSplashScreenAnimatedIcon,value,true))
        assertEquals(R.mipmap.ic_launcher,value.resourceId)
    }
}
