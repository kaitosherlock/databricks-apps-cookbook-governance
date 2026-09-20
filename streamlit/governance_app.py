"""Standalone entry point for the Cookbook governance extension."""
import streamlit as st
from governance.ui import render

st.set_page_config(page_title="Unity Catalog Governance", page_icon="🔐", layout="wide")
render()
