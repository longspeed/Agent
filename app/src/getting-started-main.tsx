import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './theme.css'
import { GettingStartedPage } from './GettingStartedPage'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <GettingStartedPage />
  </StrictMode>,
)
