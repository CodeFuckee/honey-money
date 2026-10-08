import { createApp } from 'vue'
import PrimeVue from 'primevue/config'
import carbon from './prime'
import App from './App.vue'

import 'primeicons/primeicons.css'

createApp(App)
  .use(PrimeVue, {
    theme: { preset: carbon, options: { darkModeSelector: '.app-dark' } },
    ripple: false,
  })
  .mount('#app')
