# Theme

## Compact token summary

- Institutional red: `#d90429`
- Legacy institutional blue: `#1d3b73`
- Deep slate: `#2c3e50`
- Surface: `#ffffff`
- Border: `#cccccc`
- Body typeface: Arial
- Control radius: `8px`
- Card radius: `15px`
- Card spacing: `20px`
- Existing background: diagonal gradient from deep slate to institutional red
- Breakpoints: none
- Shadows: none

The requested fingerprint panel should prioritize institutional red, white,
charcoal text, restrained gray borders, and visible focus states.

## Raw source

Source: `public/admin.html`

```css
body{
  margin:0;
  font-family:Arial;
  background:linear-gradient(135deg,#2c3e50,#d90429);
}
.header{
  background:#1d3b73;
  color:white;
  padding:15px;
  text-align:center;
}
.container{width:90%;margin:auto;}
.card{
  background:white;
  padding:20px;
  margin:20px;
  border-radius:15px;
}
input,select{
  padding:10px;
  margin:5px;
  border-radius:8px;
  border:1px solid #ccc;
}
button{
  padding:10px;
  background:#1d3b73;
  color:white;
  border:none;
  border-radius:8px;
  cursor:pointer;
}
table{
  width:100%;
  border-collapse:collapse;
}
th{
  background:#1d3b73;
  color:white;
  padding:10px;
}
td{
  padding:10px;
  text-align:center;
}
```
