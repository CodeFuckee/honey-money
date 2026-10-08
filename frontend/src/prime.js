// PrimeVue Carbon 风格预设：基于 Aura，token 全量对齐 IBM Carbon Design
// （#0f62fe 主蓝 / #f4f4f4 面板 / #161616 文字 / 方角 radius 2 / Roboto 字体栈）
import { definePreset } from '@primeuix/themes'
import Aura from '@primeuix/themes/aura'

const carbon = definePreset(Aura, {
  semantic: {
    primary: {
      50: '{blue.50}', 100: '{blue.100}', 200: '{blue.200}', 300: '{blue.300}',
      400: '#4589ff', 500: '#0f62fe', 600: '#0353e9', 700: '#0043ce',
      800: '#002d9c', 900: '#001d6c', 950: '#001141',
    },
    colorScheme: {
      light: {
        surface: {
          0: '#ffffff', 50: '#fafafa', 100: '#f4f4f4', 200: '#e0e0e0',
          300: '#c6c6c6', 400: '#a8a8a8', 500: '#8d8d8d', 600: '#6f6f6f',
          700: '#525252', 800: '#393939', 900: '#262626', 950: '#161616',
        },
        primary: { color: '#0f62fe', hoverBackground: '#0353e9', activeBackground: '#0043ce' },
        highlight: { background: '#eaf1fb', focusBackground: '#d0e2ff', textColor: '#161616' },
        content: { background: '#ffffff', hoverBackground: '#f4f4f4', borderColor: '#e0e0e0' },
        formField: {
          background: '#ffffff', hoverBackground: '#f4f4f4',
          borderColor: '#8d8d8d', hoverBorderColor: '#161616',
          focusBorderColor: '#0f62fe', activeBorderColor: '#0f62fe',
          textColor: '#161616', placeholderColor: '#a8a8a8',
        },
        text: {
          color: '#161616', hoverColor: '#161616',
          mutedColor: '#525252', hoverMutedColor: '#393939',
        },
      },
    },
  },
  components: {
    // Carbon 全系方角
    global: { borderRadius: { none: '0', xs: '0', sm: '0', md: '2px', lg: '2px', xl: '2px' } },
    button: { borderRadius: '0', fontWeight: '400', paddingX: '16px' },
    inputtext: { borderRadius: '0' },
    datatable: {
      header: { background: '#f4f4f4', color: '#525252', borderWidth: '0' },
      headerCell: { background: '#f4f4f4', hoverBackground: '#e8e8e8',
                    color: '#525252', borderWidth: '0',
                    padding: '12px 16px' },
      row: { background: '#ffffff', hoverBackground: '#f4f4f4' },
      footer: { background: '#ffffff', borderColor: '#e0e0e0' },
    },
    tag: { borderRadius: '0' },
  },
})

export default carbon
