// vite.config.js
import { defineConfig } from "file:///E:/%E5%B7%A5%E7%A8%8B/minecraftconsole/webui/node_modules/vite/dist/node/index.js";
import vue from "file:///E:/%E5%B7%A5%E7%A8%8B/minecraftconsole/webui/node_modules/@vitejs/plugin-vue/dist/index.mjs";
import { fileURLToPath, URL } from "node:url";
var __vite_injected_original_import_meta_url = "file:///E:/%E5%B7%A5%E7%A8%8B/minecraftconsole/webui/vite.config.js";
var vite_config_default = defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", __vite_injected_original_import_meta_url))
    }
  },
  base: "/",
  build: {
    outDir: "../web",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "js/app.[hash].js",
        chunkFileNames: "js/[name].[hash].js",
        manualChunks: {
          "element-plus": ["element-plus", "@element-plus/icons-vue"],
          "vue-vendor": ["vue", "vue-router"]
        },
        assetFileNames: (assetInfo) => {
          const name = assetInfo.names && assetInfo.names[0];
          if (name && /\.(css)$/.test(name)) return "css/app.[hash].css";
          if (name && /\.(png|jpe?g|gif|svg|ico)$/.test(name)) return "img/[name][extname]";
          return "css/[name][extname]";
        }
      }
    }
  }
});
export {
  vite_config_default as default
};
//# sourceMappingURL=data:application/json;base64,ewogICJ2ZXJzaW9uIjogMywKICAic291cmNlcyI6IFsidml0ZS5jb25maWcuanMiXSwKICAic291cmNlc0NvbnRlbnQiOiBbImNvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9kaXJuYW1lID0gXCJFOlxcXFxcdTVERTVcdTdBMEJcXFxcbWluZWNyYWZ0Y29uc29sZVxcXFx3ZWJ1aVwiO2NvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9maWxlbmFtZSA9IFwiRTpcXFxcXHU1REU1XHU3QTBCXFxcXG1pbmVjcmFmdGNvbnNvbGVcXFxcd2VidWlcXFxcdml0ZS5jb25maWcuanNcIjtjb25zdCBfX3ZpdGVfaW5qZWN0ZWRfb3JpZ2luYWxfaW1wb3J0X21ldGFfdXJsID0gXCJmaWxlOi8vL0U6LyVFNSVCNyVBNSVFNyVBOCU4Qi9taW5lY3JhZnRjb25zb2xlL3dlYnVpL3ZpdGUuY29uZmlnLmpzXCI7aW1wb3J0IHsgZGVmaW5lQ29uZmlnIH0gZnJvbSAndml0ZSdcbmltcG9ydCB2dWUgZnJvbSAnQHZpdGVqcy9wbHVnaW4tdnVlJ1xuaW1wb3J0IHsgZmlsZVVSTFRvUGF0aCwgVVJMIH0gZnJvbSAnbm9kZTp1cmwnXG5cbmV4cG9ydCBkZWZhdWx0IGRlZmluZUNvbmZpZyh7XG4gIHBsdWdpbnM6IFt2dWUoKV0sXG4gIHJlc29sdmU6IHtcbiAgICBhbGlhczoge1xuICAgICAgJ0AnOiBmaWxlVVJMVG9QYXRoKG5ldyBVUkwoJy4vc3JjJywgaW1wb3J0Lm1ldGEudXJsKSksXG4gICAgfSxcbiAgfSxcbiAgYmFzZTogJy8nLFxuICBidWlsZDoge1xuICAgIG91dERpcjogJy4uL3dlYicsXG4gICAgZW1wdHlPdXREaXI6IHRydWUsXG4gICAgc291cmNlbWFwOiBmYWxzZSxcbiAgICByb2xsdXBPcHRpb25zOiB7XG4gICAgICBvdXRwdXQ6IHtcbiAgICAgICAgZW50cnlGaWxlTmFtZXM6ICdqcy9hcHAuW2hhc2hdLmpzJyxcbiAgICAgICAgY2h1bmtGaWxlTmFtZXM6ICdqcy9bbmFtZV0uW2hhc2hdLmpzJyxcbiAgICAgICAgbWFudWFsQ2h1bmtzOiB7XG4gICAgICAgICAgJ2VsZW1lbnQtcGx1cyc6IFsnZWxlbWVudC1wbHVzJywgJ0BlbGVtZW50LXBsdXMvaWNvbnMtdnVlJ10sXG4gICAgICAgICAgJ3Z1ZS12ZW5kb3InOiBbJ3Z1ZScsICd2dWUtcm91dGVyJ10sXG4gICAgICAgIH0sXG4gICAgICAgIGFzc2V0RmlsZU5hbWVzOiAoYXNzZXRJbmZvKSA9PiB7XG4gICAgICAgICAgY29uc3QgbmFtZSA9IGFzc2V0SW5mby5uYW1lcyAmJiBhc3NldEluZm8ubmFtZXNbMF1cbiAgICAgICAgICBpZiAobmFtZSAmJiAvXFwuKGNzcykkLy50ZXN0KG5hbWUpKSByZXR1cm4gJ2Nzcy9hcHAuW2hhc2hdLmNzcydcbiAgICAgICAgICBpZiAobmFtZSAmJiAvXFwuKHBuZ3xqcGU/Z3xnaWZ8c3ZnfGljbykkLy50ZXN0KG5hbWUpKSByZXR1cm4gJ2ltZy9bbmFtZV1bZXh0bmFtZV0nXG4gICAgICAgICAgcmV0dXJuICdjc3MvW25hbWVdW2V4dG5hbWVdJ1xuICAgICAgICB9LFxuICAgICAgfSxcbiAgICB9LFxuICB9LFxufSlcbiJdLAogICJtYXBwaW5ncyI6ICI7QUFBOFIsU0FBUyxvQkFBb0I7QUFDM1QsT0FBTyxTQUFTO0FBQ2hCLFNBQVMsZUFBZSxXQUFXO0FBRm9JLElBQU0sMkNBQTJDO0FBSXhOLElBQU8sc0JBQVEsYUFBYTtBQUFBLEVBQzFCLFNBQVMsQ0FBQyxJQUFJLENBQUM7QUFBQSxFQUNmLFNBQVM7QUFBQSxJQUNQLE9BQU87QUFBQSxNQUNMLEtBQUssY0FBYyxJQUFJLElBQUksU0FBUyx3Q0FBZSxDQUFDO0FBQUEsSUFDdEQ7QUFBQSxFQUNGO0FBQUEsRUFDQSxNQUFNO0FBQUEsRUFDTixPQUFPO0FBQUEsSUFDTCxRQUFRO0FBQUEsSUFDUixhQUFhO0FBQUEsSUFDYixXQUFXO0FBQUEsSUFDWCxlQUFlO0FBQUEsTUFDYixRQUFRO0FBQUEsUUFDTixnQkFBZ0I7QUFBQSxRQUNoQixnQkFBZ0I7QUFBQSxRQUNoQixjQUFjO0FBQUEsVUFDWixnQkFBZ0IsQ0FBQyxnQkFBZ0IseUJBQXlCO0FBQUEsVUFDMUQsY0FBYyxDQUFDLE9BQU8sWUFBWTtBQUFBLFFBQ3BDO0FBQUEsUUFDQSxnQkFBZ0IsQ0FBQyxjQUFjO0FBQzdCLGdCQUFNLE9BQU8sVUFBVSxTQUFTLFVBQVUsTUFBTSxDQUFDO0FBQ2pELGNBQUksUUFBUSxXQUFXLEtBQUssSUFBSSxFQUFHLFFBQU87QUFDMUMsY0FBSSxRQUFRLDZCQUE2QixLQUFLLElBQUksRUFBRyxRQUFPO0FBQzVELGlCQUFPO0FBQUEsUUFDVDtBQUFBLE1BQ0Y7QUFBQSxJQUNGO0FBQUEsRUFDRjtBQUNGLENBQUM7IiwKICAibmFtZXMiOiBbXQp9Cg==
