import type { ThemeTypes } from '@/types/themeTypes/ThemeType';

const PurpleTheme: ThemeTypes = {
  name: 'PurpleTheme',
  dark: false,
  variables: {
    'border-color': '#1e88e5',
    'carousel-control-size': 10
  },
  colors: {
    primary: '#1e88e5',
    secondary: '#5e35b1',
    info: '#03c9d7',
    success: '#00c853',
    accent: '#FFAB91',
    warning: '#ffc107',
    error: '#f44336',
    lightprimary: '#eef2f6',
    lightsecondary: '#ede7f6',
    lightsuccess: '#b9f6ca',
    lighterror: '#f9d8d8',
    lightwarning: '#fff8e1',
    primaryText: '#1b1c1d',
    secondaryText: '#000000aa',
    darkprimary: '#1565c0',
    darksecondary: '#4527a0',
    borderLight: '#d0d0d0',
    border: '#d0d0d0',
    inputBorder: '#787878',
    containerBg: '#f9fafcf4',
    surface: '#fff',
    'on-surface-variant': '#fff',
    facebook: '#4267b2',
    twitter: '#1da1f2',
    linkedin: '#0e76a8',
    gray100: '#fafafacc',
    primary200: '#90caf9',
    secondary200: '#b39ddb',
    background: '#ffffff',
    overlay: '#ffffffaa',
    codeBg: '#ececec',
    preBg: 'rgb(249, 249, 249)',
    code: 'rgb(13, 13, 13)',
    chatMessageBubble: '#e7ebf4',
    mcpCardBg: '#f7f2f9',
  }
};

export { PurpleTheme };
