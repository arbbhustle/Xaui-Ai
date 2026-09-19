package com.arbnor.xauai

import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import org.json.JSONObject
import org.json.JSONArray
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import org.robolectric.RuntimeEnvironment
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import java.io.File
import java.time.Instant

@RunWith(RobolectricTestRunner::class)
@Config(sdk=[35],qualifiers="w360dp-h780dp-port-xhdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class LayoutTest {
    private fun all(view: View): List<View> = listOf(view)+if(view is ViewGroup) (0 until view.childCount).flatMap { all(view.getChildAt(it)) } else emptyList()
    private fun page(index: Int, longText: Boolean=false, legacy: Boolean=false): List<View> {
        // create() only: never onStart(), so these tests make no HTTP requests.
        val controller=Robolectric.buildActivity(MainActivity::class.java).create()
        val activity=controller.get()
        val signal=JSONObject().put("direction","NO_TRADE").put("timestamp_utc",Instant.now().toString())
            .put("data_mode","TEST_DATA")
            .put("reasons",JSONArray().put(if(longText) "Very long explanation and provider warning. ".repeat(35) else "Waiting for verified provider evidence"))
        if(legacy) signal.put("buy_score",72).put("sell_score",28).put("rsi",54).put("atr",3.25).put("regime","RANGE").put("session","LONDON")
        fun field(name: String,value: Any) { MainActivity::class.java.getDeclaredField(name).apply { isAccessible=true }.set(activity,value) }
        field("data",Dashboard(signal,null,listOf(signal),Instant.now(),emptyList())); field("page",index)
        field("message","UI validation · offline TEST_DATA")
        MainActivity::class.java.getDeclaredMethod("render",Boolean::class.javaPrimitiveType).apply { isAccessible=true }.invoke(activity,false)
        val root=activity.findViewById<View>(R.id.root)
        val density=activity.resources.displayMetrics.density
        val width=(activity.resources.configuration.screenWidthDp*density).toInt()
        val height=(activity.resources.configuration.screenHeightDp*density).toInt()
        root.measure(View.MeasureSpec.makeMeasureSpec(width,View.MeasureSpec.EXACTLY),View.MeasureSpec.makeMeasureSpec(height,View.MeasureSpec.EXACTLY)); root.layout(0,0,width,height)
        val views=all(root)
        assertTrue(views.filterIsInstance<TextView>().any { it.text.toString().contains("DEMO") })
        for(view in views) {
            assertTrue("Negative width",view.measuredWidth>=0)
            if(view is TextView && view.layout!=null) {
                assertTrue("Text clipping: ${view.text.take(50)}",view.layout.height<=view.height-view.compoundPaddingTop-view.compoundPaddingBottom)
                for(line in 0 until view.layout.lineCount) assertEquals("Ellipsized text",0,view.layout.getEllipsisCount(line))
            }
        }
        assertEquals(4,(activity.findViewById<View>(R.id.navigation) as ViewGroup).childCount)
        val bitmap=Bitmap.createBitmap(width,height,Bitmap.Config.ARGB_8888)
        val canvas=Canvas(bitmap); canvas.drawColor(Color.rgb(7,10,15)); root.draw(canvas)
        val file=File("build/reports/phase4a-ui/page-$index-${android.os.Build.VERSION.SDK_INT}-${width}-${activity.resources.configuration.fontScale}.png")
        file.parentFile?.mkdirs(); file.outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG,100,it) }; bitmap.recycle()
        controller.destroy()
        return views
    }
    @Test fun homePortraitLongExplanationsWrap() { page(0,true) }
    @Test fun signalsPortraitNoNullCrash() { page(1) }
    @Test fun analyticsPortraitInsufficientData() { assertTrue(page(2).filterIsInstance<TextView>().any { it.text.toString().contains("INSUFFICIENT_FORWARD_DATA") }) }
    @Test fun settingsPortraitShowsBrandAndDisclaimer() { assertTrue(page(3).filterIsInstance<TextView>().any { it.text.toString().contains("Powered by ArbnorSylaj") }) }
    @Test @Config(sdk=[26]) fun minimumAndroidHomeSupported() { page(0,true) }
    @Test fun launchTypographyBrand() {
        val controller=Robolectric.buildActivity(MainActivity::class.java).create()
        val texts=all(controller.get().window.decorView).filterIsInstance<TextView>().map { it.text.toString() }
        assertTrue(texts.contains("DardaniaXAUTRADE AI")); assertTrue(texts.contains("Powered by ArbnorSylaj")); controller.destroy()
    }
    @Test @Config(qualifiers="w393dp-h852dp-port-xxhdpi") fun galaxyS23ClassPortrait() { page(0,true) }
    @Test fun largeFontWrapping() { RuntimeEnvironment.setFontScale(1.5f); page(0,true) }
    @Test fun absentIntelligenceSectionsAreCompact() {
        val texts=page(0).filterIsInstance<TextView>().map { it.text.toString() }
        assertEquals(1,texts.count { it=="Advanced intelligence data is not available from the current backend." })
        assertEquals(1,texts.count { it=="Hidden-state intelligence unavailable from current backend." })
        assertEquals(1,texts.count { it=="Macro & News intelligence unavailable until the forward-data backend is enabled." })
        assertFalse(texts.any { it=="BACKEND DETAIL" || it=="SUPPORTING FACTORS" || it=="OPPOSING FACTORS" })
        assertTrue(texts.contains("Waiting for verified provider evidence"))
        assertTrue(texts.contains("BACKEND CONNECTIVITY"))
    }
    @Test fun legacyMetricsVisibleOnHome() {
        val texts=page(0,legacy=true).filterIsInstance<TextView>().map { it.text.toString() }
        for(label in listOf("BUY SCORE","SELL SCORE","RSI","ATR","REGIME","SESSION")) assertTrue(label,texts.contains(label))
        assertTrue(texts.contains("72.000")); assertTrue(texts.contains("3.250"))
    }
    private fun verifySafeAreas(bottom: Int, keyboard: Int=0) {
        val controller=Robolectric.buildActivity(MainActivity::class.java).create()
        val activity=controller.get()
        val root=activity.findViewById<ViewGroup>(R.id.root)
        val scroll=activity.findViewById<android.widget.ScrollView>(R.id.scroll)
        val nav=activity.findViewById<ViewGroup>(R.id.navigation)
        val content=activity.findViewById<ViewGroup>(R.id.content)
        val signal=JSONObject().put("direction","NO_TRADE").put("reasons",JSONArray().put("Long reason ".repeat(100)))
        MainActivity::class.java.getDeclaredField("data").apply { isAccessible=true }.set(activity,Dashboard(signal,null,listOf(signal),Instant.now(),emptyList()))
        val insets=androidx.core.view.WindowInsetsCompat.Builder()
            .setInsets(androidx.core.view.WindowInsetsCompat.Type.systemBars(),androidx.core.graphics.Insets.of(0,72,0,bottom))
            .setInsets(androidx.core.view.WindowInsetsCompat.Type.displayCutout(),androidx.core.graphics.Insets.of(12,96,12,0))
            .setInsets(androidx.core.view.WindowInsetsCompat.Type.ime(),androidx.core.graphics.Insets.of(0,0,0,keyboard)).build()
        // Display-cutout insets do not exist on API 26; it uses status-bar insets.
        val safeTop=if(android.os.Build.VERSION.SDK_INT>=28) 96 else 72
        val safeSide=if(android.os.Build.VERSION.SDK_INT>=28) 12 else 0
        for(tab in 0..3) {
            MainActivity::class.java.getDeclaredField("page").apply { isAccessible=true }.set(activity,tab)
            MainActivity::class.java.getDeclaredMethod("render",Boolean::class.javaPrimitiveType).apply { isAccessible=true }.invoke(activity,false)
            repeat(2) { androidx.core.view.ViewCompat.dispatchApplyWindowInsets(root,insets) }
            root.measure(View.MeasureSpec.makeMeasureSpec(1179,View.MeasureSpec.EXACTLY),View.MeasureSpec.makeMeasureSpec(2556,View.MeasureSpec.EXACTLY))
            root.layout(0,0,1179,2556)
            scroll.scrollTo(0,0)
            assertEquals(safeTop,root.paddingTop)
            assertEquals(safeSide,root.paddingLeft); assertEquals(safeSide,root.paddingRight)
            assertEquals(maxOf(bottom,keyboard),root.paddingBottom)
            val title=content.getChildAt(0) as TextView
            assertEquals("DardaniaXAUTRADE AI",title.text.toString())
            assertTrue("Header under status bar on tab $tab",scroll.top+content.top+title.top>=safeTop)
            assertEquals("Navigation must start after scroll viewport",scroll.bottom,nav.top)
            assertEquals(2556-maxOf(bottom,keyboard),nav.bottom)
            assertTrue(scroll.clipToPadding); assertTrue(scroll.clipChildren)
            val navTop=nav.top
            scroll.scrollTo(0,Int.MAX_VALUE)
            assertEquals(navTop,nav.top)
            assertTrue("Last content must remain above navigation",scroll.top+content.bottom-scroll.scrollY<=nav.top)
        }
        controller.destroy()
    }
    @Test fun allTabsRespectS23GestureInsetsAndCutout() { verifySafeAreas(72) }
    @Test fun allTabsRespectThreeButtonNavigationInsets() { verifySafeAreas(144) }
    @Test fun keyboardDoesNotCoverFixedNavigation() { verifySafeAreas(72,900) }
    @Test @Config(sdk=[26]) fun legacyAndroidInsetsRemainSupported() { verifySafeAreas(144) }
    @Test fun missingOptionalFieldsAreOmittedOnEveryTab() {
        for(tab in 0..3) {
            val texts=page(tab).filterIsInstance<TextView>().map { it.text.toString() }
            assertFalse("Missing-value row on tab $tab",texts.any { it=="Not available" || it=="BACKEND DETAIL" })
            if(tab==2) assertTrue(texts.contains("Not enough data"))
            if(tab==1) assertTrue(texts.any { it.contains("Legacy record") })
        }
    }
}
