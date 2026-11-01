import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({ plugins: [vue()], base: './', build: { outDir: 'dist', rollupOptions: { input: { main:'index.html', prototype:'prototype.html' } } } })
